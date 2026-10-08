# Clients and a Local Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add saved clients to LedgerSync in a local SQLite database. Each client's rows can be corrected. The plan also adds the VAT and chart fixes, statement balance checks, Excel export, and a new "Ledger" UI with a clients page and a client page.

**Architecture:**
- **Storage:** the API gains a storage layer, `ledgersync/store.py` (standard-library `sqlite3`), that keeps each row's inputs only.
- **Ledger layer:** a new `ledgersync/ledger.py` rebuilds a client's ledger on every read: `normalise` → `match` → `check_statement`, with the client's own `BusinessSettings`.
- **API:** new `/api/clients…` endpoints sit on the ledger layer. `/api/analyze` saves a finished job's rows when it is given a `client_id`.
- **Frontend:** the Next.js app gets two routes, `/` (clients) and `/clients/[id]` (one client). Both are built from small components and use the Ledger tokens in `globals.css`.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, sqlite3, openpyxl, pytest; Next.js 16.3 (app router), React 18.3, TypeScript 5.9, `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-06-clients-and-local-database-design.md`

## Global Constraints

**Dependencies and storage**
- No new Python or npm packages. SQLite goes through the standard library's `sqlite3`, and the export uses openpyxl, which is already in `requirements.txt`.
- The database file comes from `LEDGERSYNC_DB_PATH`. The default is `data/ledgersync.db` in the project folder, next to `server.py`, and `data/` is git-ignored.
- Foreign keys are on, the journal is WAL, and `PRAGMA user_version = 1`. The file is created on first use, not when the API starts.
- Rows are saved as inputs only. The outputs are never saved and are worked out on every read: `vat_posted`, `net`, `paid_against`, `pays`, `candidates`, `owed`, `paid_by` and the derived issues.
- Times are stored as UTC ISO-8601 strings.

**API behaviour**
- Errors keep the shape `{"detail": {"code", "message"}}`:
  - 404 `not_found`;
  - 409 `client_archived`;
  - 422 `invalid_input`;
  - 500 `storage_error`.

  Never log document contents.
- The API keeps listening on 127.0.0.1 only.
- These keep their requests and responses:
  - `/api/transactions/validate` and `/api/trial-balance`;
  - `/api/accounts` without a query;
  - `/api/health`;
  - the eval runner.

**Code rules**
- Account definitions stay at most 110 characters (`tests/test_accounts.py`).
- Python 3.11: no backslashes or reused quote marks inside f-string expressions.

**The Ledger look**

| Token | Light | Dark |
|---|---|---|
| Page | `#FAF8F3` | `#151714` |
| Card | `#FFFFFF` | `#1D201C` |
| Text | `#1F2421` | `#ECEAE3` |
| Muted | `#5F5E5A` | `#A3A199` |
| Rule | `#E4E0D6` | `#30342E` |
| Accent | `#0F6E56` | `#1D9E75` |
| Accent tint | `#E1F5EE` | `#085041` |
| Text on tint | `#085041` | `#9FE1CB` |

- Fonts load through `next/font/google`:
  - Source Serif 4 for headings;
  - Inter for text;
  - IBM Plex Mono, weights 400 and 500, for figures with tabular numbers.
- The theme choice is kept in `localStorage` under `ledgersync-theme`.
- Pages work at 1024 px wide without the page scrolling sideways.

**Copy**
- Copy is sentence case.
- These strings are exact:
  - "Archive {name}? You can restore it from Show archived."
  - "Remove {name} and its {n} row(s)? This can't be undone: analyse the file again to get them back."
  - "Archived. Restore to add documents or make changes."
  - "Skipped: same file as {name}, uploaded {6 Oct}"
  - "Balances add up", "Doesn't add up: £{x}", "No balances to check"
  - "Enter the company name", "Enter the responsible person", "Enter a valid email"
  - "Client not found"

**Next.js 16**
- Client pages read route params with `useParams()` from `next/navigation`, because React 18.3 has no `use()`.
- Before using any other Next API, read its page in `frontend/node_modules/next/dist/docs/`.

**Process**
- Commit after each task on `robust-ledger`. End every message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python tests: `.venv/bin/python -m pytest -q`. The whole suite is green at the start, with 409 passed.
- Frontend checks: `cd frontend && npm test` and `npx tsc --noEmit`.

## Review Focus

1. **A job that finishes after its client was archived:** its rows are still saved. Task 9: `test_a_job_that_finishes_after_its_client_was_archived_is_still_saved`.
2. **A background job saving rows while a person changes rows:** no "database is locked". Task 6: `test_jobs_and_people_can_write_at_the_same_time`.
3. **A client name that a file name can't hold, or with non-ASCII letters, in the Excel download.** Task 10: `test_the_file_name_drops_characters_file_names_cannot_hold` and `test_the_export_downloads_with_the_clients_name_even_when_archived`.
4. **A statement listed newest first, or printing one balance a day:** no false gaps. Task 4: `test_a_statement_listed_newest_first_adds_up_too` and `test_a_balance_printed_once_a_day_is_followed_across_the_lines_without_one`.
5. **Editing one row of a multi-row document:** type and counterparty stay the same across the document, and Revert brings the whole document back. Task 6: `test_include_and_document_wide_fields_change_every_row_of_the_document` and `test_a_rows_first_edit_keeps_what_it_held_and_revert_brings_the_document_back`.

## File structure

**Backend:**
- `ledgersync/accounts.py`: the chart changes, `model_accounts()` and `choosable(business_type)`.
- `ledgersync/models.py`:
  - `BusinessType`;
  - the balances on `Transaction`;
  - `StatementCheck`;
  - the client and ledger API models.
- `ledgersync/checks.py`: `vat_blocked`, `director_loan`, `STATEMENT_ISSUES` and `is_derived`.
- `ledgersync/extractor.py` and `ledgersync/adapter.py`: the business type in the prompt, and the statement balances read and carried.
- `ledgersync/statements.py` (new): `check_statement`.
- `ledgersync/config.py`: `db_path`.
- `ledgersync/errors.py`:
  - `NotFound`, `ClientArchived`, `InvalidInput` and `StorageError`;
  - `InvalidTransactions.problems`.
- `ledgersync/store.py` (new): `Store`.
- `ledgersync/ledger.py` (new): a client's ledger, and a person's changes to it.
- `ledgersync/export.py` (new): the workbook.
- `ledgersync/posting.py`: the problems carried on the error.
- `server.py`: the endpoints.
- `tests/`:
  - existing: `test_accounts.py`, `test_checks.py`, `test_extractor.py`, `test_adapter.py`, `test_config.py`;
  - new: `test_statements.py`, `test_store.py`, `test_ledger.py`, `test_clients_api.py`, `test_export.py`.

**Frontend (`frontend/src`):**
- `lib/api.ts`: `request` is exported, and `AnalyzeResult` gains `upload_id`.
- `lib/clients.ts` (new).
- `lib/clientRules.ts` (new), with `clientRules.test.ts`.
- `lib/theme.ts` (new), with `theme.test.ts`.
- `app/`:
  - `globals.css` (rewritten);
  - `layout.tsx`;
  - `page.tsx` (the clients page);
  - `clients/[id]/page.tsx` (new).
- `components/` (all new): `AppHeader.tsx`, `ClientDialog.tsx`, `TransactionsTable.tsx`, `UploadsList.tsx`, `TrialBalancePanel.tsx`, `AddDocuments.tsx` and `EditRowDialog.tsx`.
- `frontend/package.json`: the `test` script.

**Other:**
- `.gitignore`: adds `data/`.
- `README.md`.
- `.claude/launch.json`: git-ignored. It gains a spare-port API entry that uses a scratch database.

---

### Task 1: Chart — blocked VAT, cars and vans, a director's loan account, accounts by business type

**Files:**
- Modify: `ledgersync/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Produces:
  - `Account.reclaim_vat: bool` (default `True`);
  - `DIRECTORS_LOAN, CAPITAL, DRAWINGS = "2250", "3000", "3260"`;
  - `model_accounts() -> tuple[Account, ...]`: every account a row can be coded to, whatever the business;
  - `choosable(business_type: Optional[str] = None) -> tuple[Account, ...]`.
- Chart changes:
  - `0050 Cars` blocks VAT;
  - new `0055 Vans`;
  - new `2250 Director's Loan Account`;
  - `7403 Entertainment` blocks VAT.

- [ ] **Step 1: Write the failing tests**

In `tests/test_accounts.py`, change the import line to:

```python
from ledgersync.accounts import BANK, BY_CODE, CHART, AccountType, choosable, model_accounts
```

Replace `test_a_model_may_choose_any_account_but_the_bank_and_the_control_accounts` with:

```python
def test_a_model_may_choose_any_account_but_the_bank_and_the_control_accounts():
    # The bank is the other side of every posting, and the ledger splits VAT itself. What is owed
    # (Debtors, Creditors, Expenses Owed to Staff) is picked by the ledger from the document type.
    ledger_only = {"1200", "2200", "2201", "1100", "2100", "2110"}
    assert {a.code for a in model_accounts()} == set(BY_CODE) - ledger_only
    # Without a business type (an analysis with no client) the director's loan account is left out.
    assert {a.code for a in choosable()} == set(BY_CODE) - ledger_only - {"2250"}
```

Append:

```python
def test_vat_on_entertainment_and_cars_is_blocked_but_vans_reclaim_it():
    # UK VAT: business entertainment, and cars not used only for business, can't have their VAT reclaimed.
    assert [code for code, a in BY_CODE.items() if not a.reclaim_vat] == ["0050", "7403"]
    assert (BY_CODE["0050"].name, BY_CODE["0055"].name, BY_CODE["0055"].type) == ("Cars", "Vans", AccountType.ASSET)


def test_a_limited_company_books_its_owners_through_the_directors_loan_account():
    company = {a.code for a in choosable("limited_company")}
    assert "2250" in company and not {"3000", "3260"} & company
    for kind in ("sole_trader", "partnership", "llp"):
        others = {a.code for a in choosable(kind)}
        assert {"3000", "3260"} <= others and "2250" not in others
    assert (BY_CODE["2250"].name, BY_CODE["2250"].type) == ("Director's Loan Account", AccountType.LIABILITY)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_accounts.py`
Expected: FAIL at collection with `ImportError: cannot import name 'model_accounts'`.

- [ ] **Step 3: Change the chart**

In `ledgersync/accounts.py`:

1. Add `from typing import Optional` under `from enum import Enum`.
2. Give `Account` a last field:

```python
    reclaim_vat: bool = True   # False: VAT on a purchase here can't be reclaimed, so it stays in the cost
```

3. In `CHART`, replace the `0050` line with these two lines:

```python
    Account("0050", "Cars", _A, V.STANDARD,
            "Cars bought for the business; VAT on a car can't be reclaimed unless it is used only for business.",
            reclaim_vat=False),
    Account("0055", "Vans", _A, V.STANDARD, "Vans and other commercial vehicles bought for the business."),
```

4. After the `2210` line, add:

```python
    Account("2250", "Director's Loan Account", _L, V.OUTSIDE_SCOPE,
            "Money a director has lent the company, or taken from it for personal use. Limited companies only."),
```

5. Replace the `7403` line with:

```python
    Account("7403", "Entertainment", _X, V.STANDARD,
            "Meals and hospitality for clients or other guests; VAT on it can't be reclaimed.", reclaim_vat=False),
```

6. Replace everything from `BANK, SALES_VAT, PURCHASE_VAT, SUSPENSE = ...` to the end of the file with:

```python
BANK, SALES_VAT, PURCHASE_VAT, SUSPENSE = "1200", "2200", "2201", "9998"
DEBTORS, CREDITORS, STAFF_EXPENSES = "1100", "2100", "2110"
DIRECTORS_LOAN, CAPITAL, DRAWINGS = "2250", "3000", "3260"
# Picked by the ledger, never chosen for a row: the bank (the other side of each posting), the VAT control
# accounts (the ledger splits VAT itself) and the accounts for what is owed (from the document type).
_LEDGER_ONLY = frozenset({BANK, SALES_VAT, PURCHASE_VAT, DEBTORS, CREDITORS, STAFF_EXPENSES})


def model_accounts() -> tuple[Account, ...]:
    """Every account a row can be coded to, whatever the kind of business."""
    return tuple(a for a in CHART if a.code not in _LEDGER_ONLY)


def choosable(business_type: Optional[str] = None) -> tuple[Account, ...]:
    """The accounts offered for one business. A limited company's owners go through the director's loan
    account; anyone else's through Drawings and Capital Introduced, as when the kind of business isn't
    known (an analysis without a client)."""
    hidden = {CAPITAL, DRAWINGS} if business_type == "limited_company" else {DIRECTORS_LOAN}
    return tuple(a for a in model_accounts() if a.code not in hidden)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_accounts.py`
Expected: PASS. All definitions are still at most 110 characters.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add ledgersync/accounts.py tests/test_accounts.py
git commit -m "Chart: no VAT reclaimed on entertainment or cars, a Vans account, and a director's loan account for limited companies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Ledger checks — business type, blocked VAT and the director's loan account

**Files:**
- Modify: `ledgersync/models.py` (`BusinessType`, `BUSINESS_TYPES`, `BusinessSettings.business_type`)
- Modify: `ledgersync/checks.py` (`normalise`, `_DERIVED`, `is_derived`)
- Test: `tests/test_checks.py`

**Interfaces:**
- Consumes: from Task 1, `Account.reclaim_vat`, `DIRECTORS_LOAN`, `CAPITAL` and `DRAWINGS`.
- Produces:
  - `models.BusinessType = Literal["limited_company", "sole_trader", "partnership", "llp"]` and `models.BUSINESS_TYPES`;
  - `BusinessSettings.business_type: Optional[BusinessType] = None`;
  - the issue codes `vat_blocked` (info) and `director_loan` (warning), both derived;
  - `checks.is_derived(issue: Issue) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checks.py`:

```python
LIMITED = BusinessSettings(business_type="limited_company")
TRADER = BusinessSettings(business_type="sole_trader")


def test_vat_on_business_entertainment_stays_in_the_cost():
    # Client sandwiches with £6.37 VAT on the receipt: UK rules block that VAT, so it is part of the cost.
    t = normalise(tx(account="7403", gross="38.20", vat="6.37"), REGISTERED)
    assert (t.vat_posted, t.net) == (Decimal("0.00"), Decimal("38.20"))
    assert codes(t) == [("vat_blocked", "info")]
    assert t.issues[0].message == "VAT on Entertainment can't be reclaimed, so the £6.37 stays in the cost."
    assert codes(normalise(t, REGISTERED)) == [("vat_blocked", "info")]   # validating again doesn't repeat it


def test_a_cars_vat_stays_in_its_cost_but_a_vans_is_reclaimed():
    car = normalise(tx(account="0050", gross="24000.00", vat="4000.00"), REGISTERED)
    van = normalise(tx(account="0055", gross="24000.00", vat="4000.00"), REGISTERED)
    assert (car.vat_posted, car.net) == (Decimal("0.00"), Decimal("24000.00"))
    assert (van.vat_posted, van.net) == (Decimal("4000.00"), Decimal("20000.00"))


def test_a_limited_companys_owners_go_through_the_directors_loan_account():
    drawings = normalise(tx(account="3260", gross="500.00"), LIMITED)
    assert codes(drawings) == [("director_loan", "warning")]
    assert drawings.issues[0].message == "For a limited company, use 2250 Director's Loan Account instead of Drawings."
    assert codes(normalise(tx(account="2250", gross="500.00"), LIMITED)) == []


def test_a_sole_trader_has_no_directors_loan_account():
    assert codes(normalise(tx(account="2250", gross="500.00"), TRADER)) == [("director_loan", "warning")]
    assert codes(normalise(tx(account="3260", gross="500.00"), TRADER)) == []


def test_without_a_business_type_the_owners_accounts_are_not_questioned():
    assert codes(normalise(tx(account="3260", gross="500.00"), REGISTERED)) == []
    assert codes(normalise(tx(account="2250", gross="500.00"), REGISTERED)) == []
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_checks.py`
Expected: 4 FAIL:
- entertainment: `vat_posted` is `6.37`;
- the car: `vat_posted` is `4000.00`;
- the two `director_loan` tests: `codes(...)` is `[]`, because Pydantic drops the unknown `business_type` field.

The fifth passes, as nothing questions the owners' accounts yet.

- [ ] **Step 3: Add the business type to the settings**

In `ledgersync/models.py`, after `NOT_TRANSACTIONS = frozenset(DOCUMENT_TYPES[4:])`, add:

```python
# The kind of business a client is: a limited company's owners go through 2250 Director's Loan Account,
# anyone else's through 3260 Drawings and 3000 Capital Introduced.
BusinessType = Literal["limited_company", "sole_trader", "partnership", "llp"]
BUSINESS_TYPES: tuple[str, ...] = get_args(BusinessType)
```

In `BusinessSettings`, after `vat_registered: bool = True`, add:

```python
    business_type: Optional[BusinessType] = None   # None: not known, as for an analysis without a client
```

- [ ] **Step 4: Book blocked VAT and question the owners' accounts**

In `ledgersync/checks.py`, change the accounts import to:

```python
from .accounts import BY_CODE, CAPITAL, CREDITORS, DEBTORS, DIRECTORS_LOAN, DRAWINGS, STAFF_EXPENSES, AccountType
```

Add `"vat_blocked", "director_loan"` to `_DERIVED`:

```python
_DERIVED = {"unknown_account", "same_account", "non_gbp_currency", "vat_not_applicable", "vat_estimated",
            "vat_arithmetic", "vat_rate_mismatch", "date_missing", "date_out_of_period", "unusual_direction",
            "not_booked", "vat_blocked", "director_loan"} | MATCHING_ISSUES
```

After `def issue(...)`, add:

```python
def is_derived(found: Issue) -> bool:
    """An issue the ledger works out afresh on every pass; such issues are never saved."""
    return found.code in _DERIVED
```

In `normalise`, insert a new branch right after the `_NO_VAT` branch, before `elif vat is None and tx.vat_treatment is None:`:

```python
    elif account is not None and not account.reclaim_vat and tx.direction == Direction.OUT:
        # Business entertainment, or a car: the VAT can't be reclaimed, so it stays in the cost.
        if vat:
            issues.append(issue("vat_blocked", f"VAT on {account.name} can't be reclaimed, so the £{vat} stays "
                                                "in the cost.", "info"))
        posted = ZERO
```

In `normalise`, insert just before `if not booked(tx):`:

```python
    if settings.business_type == "limited_company" and account is not None and tx.account_code in (CAPITAL, DRAWINGS):
        issues.append(issue("director_loan", f"For a limited company, use 2250 Director's Loan Account instead of "
                                             f"{account.name}."))
    elif settings.business_type not in (None, "limited_company") and tx.account_code == DIRECTORS_LOAN:
        issues.append(issue("director_loan", "Director's Loan Account is for limited companies; use 3260 Drawings "
                                             "or 3000 Capital Introduced."))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_checks.py`
Expected: PASS.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

The eval's client-sandwich case, coded 7403, expects no VAT. The eval scores VAT only where a case expects some (`eval_scoring.score_case`), so blocked VAT doesn't change that case and no eval edit is needed. This answers the spec's conditional.

- [ ] **Step 6: Commit**

```bash
git add ledgersync/models.py ledgersync/checks.py tests/test_checks.py
git commit -m "Ledger: keep blocked VAT in the cost, and flag owners' accounts that don't fit the kind of business

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The model gets the business type and reads statement balances

**Files:**
- Modify: `ledgersync/models.py` (`Transaction.balance`, `opening_balance`, `closing_balance`)
- Modify: `ledgersync/extractor.py`:
  - `ACCOUNT_CHOICES` and `BUSINESS_WORDS`;
  - the new fields;
  - the `business_type` argument;
  - rule 5 and the answer's shape.
- Modify: `ledgersync/adapter.py` (carry the balances on bank lines)
- Test: `tests/test_extractor.py`, `tests/test_adapter.py`

**Interfaces:**
- Consumes: from Task 1, `model_accounts()` and `choosable(business_type)`.
- Produces:
  - `TransactionExtractor(client, business_name="", business_type=None)`;
  - the inputs `Transaction.balance`, `opening_balance` and `closing_balance` as `Optional[Decimal]`, set on `statement` rows only;
  - the model row fields `balance`, `opening_balance` and `closing_balance`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
def test_the_prompt_names_a_limited_company_and_offers_its_directors_loan_account():
    client = FakeModel([transactions_json(ROW)])
    TransactionExtractor(client, business_name="Business Cube Ltd",
                         business_type="limited_company").extract_accounting_data("x")
    system = client.calls[0]["messages"][0]["content"]
    assert "You keep the books of Business Cube Ltd, a UK limited company." in system
    assert "2250 Director's Loan Account:" in system and "3260 Drawings:" not in system


def test_a_sole_trader_is_offered_drawings_and_no_directors_loan():
    client = FakeModel([transactions_json(ROW)])
    TransactionExtractor(client, business_name="Jo Bloggs", business_type="sole_trader").extract_accounting_data("x")
    system = client.calls[0]["messages"][0]["content"]
    assert "You keep the books of Jo Bloggs, a UK sole trader." in system
    assert "3260 Drawings:" in system and "2250 Director's Loan Account:" not in system


def test_the_schema_accepts_any_account_a_business_could_use():
    account = ExtractedTransactions.model_json_schema()["$defs"]["AccountingTransaction"]["properties"]["account"]
    assert {"2250 Director's Loan Account", "3260 Drawings", "0055 Vans"} <= set(account["enum"])


def test_statement_rows_carry_their_printed_balances():
    row = dict(ROW, balance=1240.5, opening_balance=1500.0, closing_balance=-20.0)
    result, client = extract(transactions_json(row))
    assert (result.data[0].balance, result.data[0].opening_balance, result.data[0].closing_balance) == (
        1240.5, 1500.0, -20.0)
    assert "opening_balance and closing_balance on every row" in client.calls[0]["messages"][0]["content"]
```

Append to `tests/test_adapter.py`:

```python
def test_a_bank_line_keeps_the_balances_its_statement_prints():
    [t] = adapt(dict(ROW, document_type="statement", balance=1240.5, opening_balance=1500, closing_balance=-20))
    assert (t.balance, t.opening_balance, t.closing_balance) == (Decimal("1240.50"), Decimal("1500.00"), Decimal("-20.00"))


def test_balances_on_any_other_row_are_dropped():
    [t] = adapt(dict(ROW, document_type="receipt", balance=10, opening_balance=5, closing_balance=1))
    assert (t.balance, t.opening_balance, t.closing_balance) == (None, None, None)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_extractor.py tests/test_adapter.py`
Expected: FAIL:
- `TypeError: ... unexpected keyword argument 'business_type'`;
- the enum lacks `2250 Director's Loan Account`;
- `AttributeError: 'AccountingTransaction' object has no attribute 'balance'`;
- `AttributeError: 'Transaction' object has no attribute 'balance'`.

- [ ] **Step 3: Add the balances to `Transaction`**

In `ledgersync/models.py`, in `Transaction`, after the `link` field and its comment, add:

```python
    # Inputs too, on a bank statement row: the running balance printed on its line and the statement's
    # opening and closing balances, which statements.check_statement compares with the rows.
    balance: Optional[Decimal] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
```

In the same class, change the money validator's decorator to:

```python
    @field_validator("vat", "vat_posted", "net", "owed", "balance", "opening_balance", "closing_balance", mode="before")
```

- [ ] **Step 4: Tell the model the business type, and ask for the balances**

In `ledgersync/extractor.py`:

1. Change the accounts import to `from .accounts import choosable, model_accounts`.
2. Replace the `ACCOUNT_CHOICES` comment and line with:

```python
# Every account a model may name, for any kind of business, as "<code> <name>"; the prompt lists the ones
# for the business being read.
ACCOUNT_CHOICES: tuple[str, ...] = tuple(f"{a.code} {a.name}" for a in model_accounts())
```

3. Under `MODEL_DOCUMENT_TYPES`, add:

```python
# How the prompt names each kind of business (models.BusinessType).
BUSINESS_WORDS = {"limited_company": "limited company", "sole_trader": "sole trader",
                  "partnership": "partnership", "llp": "limited liability partnership"}
```

4. In `AccountingTransaction`, after `counterparty`, add:

```python
    # On a bank statement row: the balances it prints, so the ledger can check no line was missed or misread.
    balance: Optional[float] = Field(None, description="On a bank statement row: the running balance printed on "
                                                       "its line, after it; null when the line shows none, and on "
                                                       "every other kind of row")
    opening_balance: Optional[float] = Field(None, description="On a bank statement row: the statement's opening "
                                                               "balance (brought forward), the same on every row; null "
                                                               "when not printed, and on every other kind of row")
    closing_balance: Optional[float] = Field(None, description="On a bank statement row: the statement's closing "
                                                               "balance (carried forward), the same on every row; null "
                                                               "when not printed, and on every other kind of row")
```

5. Replace `TransactionExtractor.__init__` with:

```python
    def __init__(self, client, business_name: str = "", business_type: Optional[str] = None):
        self.client = client
        self.business_name = business_name
        self.business_type = business_type   # models.BusinessType, or None when not known
```

6. In `_instructions`, replace the first two lines of the body (`name = ...` and `whose = ...`) with:

```python
        name = self.business_name
        kind = f"a UK {BUSINESS_WORDS[self.business_type]}" if self.business_type in BUSINESS_WORDS else "a UK business"
        whose = f"{name}, {kind}" if name else kind
```

7. In the same method, change `chart = ...` to:

```python
        chart = "\n".join(f"{a.code} {a.name}: {a.definition}" for a in choosable(self.business_type))
```

8. Replace rule 5 of the prompt with this single line:

```
5. A bank statement or spreadsheet has one transaction per payment row. The amount is the money that moved, as a positive number; take the direction from the paid in / paid out columns or the sign. Balances and totals are not transactions, and document_total and document_vat are null. On a bank statement, give the balance printed on each line as balance, and the statement's opening balance (brought forward) and closing balance (carried forward) as opening_balance and closing_balance on every row; use null for any it does not print. An overdrawn balance (shown with OD, D or a minus sign) is negative.
```

9. In the answer-shape line at the end of the prompt, replace `"counterparty": "who was paid or who paid"}}]}}` with:

```
"counterparty": "who was paid or who paid", "balance": 1250.00 or null, "opening_balance": 1500.00 or null, "closing_balance": 980.50 or null}}]}}
```

- [ ] **Step 5: Carry the balances on bank lines**

In `ledgersync/adapter.py`, in `to_transactions`, add this line right before `tx = Transaction(`:

```python
        statement = _document_type(row) == "statement"
```

Then add three arguments at the end of the `Transaction(...)` call, after `document_ref=...`:

```python
                         balance=to_money(row.get("balance")) if statement else None,
                         opening_balance=to_money(row.get("opening_balance")) if statement else None,
                         closing_balance=to_money(row.get("closing_balance")) if statement else None)
```

The existing closing parenthesis after `document_ref=...` moves to the end of the last of these three lines.

- [ ] **Step 6: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_extractor.py tests/test_adapter.py`
Expected: PASS. `test_the_instructions_spell_out_every_field_of_the_answer` passes too, because the answer shape names the three new fields.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 7: Commit**

```bash
git add ledgersync/models.py ledgersync/extractor.py ledgersync/adapter.py tests/test_extractor.py tests/test_adapter.py
git commit -m "Model: told the kind of business, offered its owners' accounts, and reads statement balances

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 8: Run the affected eval cases against Groq**

The prompt changed, so measure it, as with document types on 2026-10-06.

1. Start the API with `preview_start` and the existing `.claude/launch.json` entry `api-groq-eval-8087`. An analysis without a client never opens the database.
2. Run:

```bash
.venv/bin/python eval/run_eval.py --api http://127.0.0.1:8087 --label statement-balances --cases csv- --pause 15 --note "statement balances in the prompt; business type; reasoning low; 1 image; Northbridge"
```

3. Repeat with `--resume`, once for each of `--cases pdf-bank`, `--cases text-pasted-csv`, `--cases xlsx` and `--cases receipt`.
4. If a case is `ai_rate_limited`, run the same command again with `--resume` once the minute has passed.

Expected: correct rows 1.0 on every case that finishes, as on 2026-10-06. Report every case whose rows, accounts or types changed.

Stop the server afterwards, then commit:

```bash
git add eval/results/
git commit -m "Eval: statement balances and the business type in the prompt

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The statement balance check

**Files:**
- Modify: `ledgersync/checks.py` (`STATEMENT_ISSUES` joins `_DERIVED`)
- Modify: `ledgersync/models.py` (`StatementCheck`)
- Create: `ledgersync/statements.py`
- Test: `tests/test_statements.py`

**Interfaces:**
- Consumes: from Task 3, `Transaction.balance`, `opening_balance` and `closing_balance`.
- Produces:
  - `models.StatementCheck(status: Literal["ok", "gap", "none"], difference: Optional[Decimal] = None)`;
  - `checks.STATEMENT_ISSUES = {"statement_gap", "statement_total"}`;
  - `statements.check_statement(rows: list[Transaction]) -> tuple[list[Transaction], Optional[StatementCheck]]`. The rows are one upload's rows, in order. The summary is `None` when the upload has no `statement` rows.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_statements.py`:

```python
import datetime as dt
from decimal import Decimal

from ledgersync.models import Transaction
from ledgersync.statements import check_statement


def line(direction, gross, balance=None, opening=None, closing=None, kind="statement"):
    return Transaction(direction=direction, gross=gross, account_code="7502", date=dt.date(2026, 9, 1),
                       description="line", document_type=kind, balance=balance,
                       opening_balance=opening, closing_balance=closing)


def test_a_skipped_line_is_flagged_where_the_balance_breaks():
    # Opening £1,000.00; out £20, out £60, [out £12.40 skipped by the model], in £100; closing £1,007.60.
    both = dict(opening="1000.00", closing="1007.60")
    rows = [line("out", "20.00", "980.00", **both), line("out", "60.00", "920.00", **both),
            line("in", "100.00", "1007.60", **both)]
    checked, summary = check_statement(rows)
    assert (summary.status, summary.difference) == ("gap", Decimal("12.40"))
    assert [[i.code for i in tx.issues] for tx in checked] == [[], [], ["statement_gap", "statement_total"]]
    assert checked[2].issues[0].message == ("The balance after this line should be £1,020.00; "
                                            "the statement shows £1,007.60.")
    assert checked[2].issues[1].message == ("Opening £1,000.00 plus these rows gives £1,020.00, but the "
                                            "statement closes at £1,007.60 (£12.40 apart).")


def test_a_statement_that_adds_up_is_ok():
    both = dict(opening="1000.00", closing="1030.00")
    checked, summary = check_statement([line("in", "100.00", "1100.00", **both), line("out", "70.00", "1030.00", **both)])
    assert (summary.status, summary.difference) == ("ok", None) and [tx.issues for tx in checked] == [[], []]


def test_a_statement_listed_newest_first_adds_up_too():
    rows = [line("in", "100.00", "1007.60"), line("out", "12.40", "907.60"), line("out", "60.00", "920.00"),
            line("out", "20.00", "980.00", opening="1000.00", closing="1007.60")]
    checked, summary = check_statement(rows)
    assert summary.status == "ok" and all(not tx.issues for tx in checked)


def test_a_balance_printed_once_a_day_is_followed_across_the_lines_without_one():
    rows = [line("out", "20.00", "980.00"), line("out", "30.00"), line("out", "50.00", "900.00")]
    assert check_statement(rows)[1].status == "ok"


def test_an_overdrawn_balance_reads_as_a_minus():
    [_, second], summary = check_statement([line("out", "50.00", "-30.00"), line("out", "20.00", "-60.00")])
    assert second.issues[0].message == "The balance after this line should be -£50.00; the statement shows -£60.00."
    assert (summary.status, summary.difference) == ("gap", Decimal("10.00"))


def test_a_statement_without_balances_has_nothing_to_check():
    checked, summary = check_statement([line("out", "20.00"), line("in", "5.00")])
    assert (summary.status, summary.difference) == ("none", None)


def test_only_bank_lines_are_checked_and_an_upload_without_any_has_no_summary():
    receipt = line("out", "20.00", kind="receipt")
    assert check_statement([receipt]) == ([receipt], None)
    checked, summary = check_statement([receipt, line("out", "20.00", "980.00"), line("out", "30.00", "950.00")])
    assert summary.status == "ok" and checked[0] is receipt


def test_checking_again_replaces_the_earlier_warnings():
    once, _ = check_statement([line("out", "20.00", "980.00"), line("out", "30.00", "900.00")])
    twice, _ = check_statement(once)
    assert [len(tx.issues) for tx in twice] == [0, 1]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_statements.py`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'ledgersync.statements'`.

- [ ] **Step 3: Add the summary model and the issue codes**

In `ledgersync/models.py`, after the `Settlement` class, add:

```python
class StatementCheck(BaseModel):
    """Whether a bank statement's rows agree with the balances it prints: ok, a gap of `difference`, or none
    when it prints no balances to compare."""
    status: Literal["ok", "gap", "none"]
    difference: Optional[Decimal] = None
```

In `ledgersync/checks.py`, under `MATCHING_ISSUES`, add:

```python
# Issues statements.check_statement adds to bank lines; derived too, so checking again replaces them.
STATEMENT_ISSUES = {"statement_gap", "statement_total"}
```

Change the end of the `_DERIVED` definition from `| MATCHING_ISSUES` to `| MATCHING_ISSUES | STATEMENT_ISSUES`.

- [ ] **Step 4: Write the check**

Create `ledgersync/statements.py`:

```python
"""Checks a bank statement's rows against the balances it prints (design of 2026-10-06). Each line's running
balance must follow from the line before, and the opening balance plus the rows must reach the closing
balance; a gap means a line was missed or misread. Pure functions over one upload's rows, in their order."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .checks import STATEMENT_ISSUES, issue
from .models import Direction, Issue, StatementCheck, Transaction
from .money import ZERO


def _signed(tx: Transaction) -> Decimal:
    return tx.gross if tx.direction == Direction.IN else -tx.gross


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}" if amount >= 0 else f"-£{-amount:,.2f}"


def check_statement(rows: list[Transaction]) -> tuple[list[Transaction], Optional[StatementCheck]]:
    """One upload's rows, its bank lines carrying statement_gap or statement_total warnings where they don't
    agree with the printed balances, and the upload's summary; the summary is None when it has no bank lines."""
    lines = [n for n, tx in enumerate(rows) if tx.document_type == "statement"]
    if not lines:
        return rows, None
    found: dict[int, list[Issue]] = {}
    gaps: list[Decimal] = []
    compared = 0

    def moved(start: int, stop: int) -> Decimal:
        """The money in less the money out of lines[start:stop]."""
        return sum((_signed(rows[lines[k]]) for k in range(start, stop)), ZERO)

    def fits(expected: list[tuple[int, Decimal]]) -> int:
        return sum(rows[lines[k]].balance == want for k, want in expected)

    # Running balances, from one printed balance to the next (some statements print one a day). A statement
    # may list its lines oldest or newest first; the reading that fits more of them is used.
    shown = [k for k, n in enumerate(lines) if rows[n].balance is not None]
    pairs = list(zip(shown, shown[1:]))
    oldest_first = [(q, rows[lines[p]].balance + moved(p + 1, q + 1)) for p, q in pairs]
    newest_first = [(p, rows[lines[q]].balance + moved(p, q)) for p, q in pairs]
    for k, want in newest_first if fits(newest_first) > fits(oldest_first) else oldest_first:
        compared += 1
        printed = rows[lines[k]].balance
        if printed != want:
            gaps.append(abs(want - printed))
            found.setdefault(lines[k], []).append(issue(
                "statement_gap",
                f"The balance after this line should be {_gbp(want)}; the statement shows {_gbp(printed)}."))

    # The opening balance plus every line must reach the closing balance.
    openings = [rows[n].opening_balance for n in lines if rows[n].opening_balance is not None]
    closings = [rows[n].closing_balance for n in lines if rows[n].closing_balance is not None]
    if openings and closings:
        compared += 1
        total = openings[0] + moved(0, len(lines))
        if total != closings[-1]:
            apart = abs(total - closings[-1])
            gaps.insert(0, apart)
            found.setdefault(lines[-1], []).append(issue(
                "statement_total",
                f"Opening {_gbp(openings[0])} plus these rows gives {_gbp(total)}, but the statement closes at "
                f"{_gbp(closings[-1])} ({_gbp(apart)} apart)."))

    on_lines = set(lines)
    checked = [tx.model_copy(update={"issues": [i for i in tx.issues if i.code not in STATEMENT_ISSUES]
                                               + found.get(n, [])}) if n in on_lines else tx
               for n, tx in enumerate(rows)]
    if not compared:
        return checked, StatementCheck(status="none")
    return checked, StatementCheck(status="gap", difference=gaps[0]) if gaps else StatementCheck(status="ok")
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_statements.py`
Expected: PASS (8 tests).

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add ledgersync/checks.py ledgersync/models.py ledgersync/statements.py tests/test_statements.py
git commit -m "Statements: check the rows against the opening, running and closing balances printed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The store — settings, errors and clients

**Files:**
- Modify: `ledgersync/config.py` (`DEFAULT_DB`, `Settings.db_path`, `LEDGERSYNC_DB_PATH`)
- Modify: `ledgersync/errors.py` (`NotFound`, `ClientArchived`, `InvalidInput`, `StorageError`)
- Modify: `ledgersync/models.py` (`ClientFields`, `Client`, `ClientPatch`)
- Modify: `.gitignore` (`data/`)
- Create: `ledgersync/store.py`
- Test: `tests/test_store.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: from Task 2, `models.BusinessType`.
- Produces:
  - `Settings.db_path: Path`;
  - errors `NotFound` (404 `not_found`), `ClientArchived` (409 `client_archived`), `InvalidInput` (422 `invalid_input`) and `StorageError` (500 `storage_error`);
  - `ClientFields(name, business_type, contact_name, contact_email="", contact_phone="", vat_registered=True)`, whose text fields are trimmed;
  - `Client(ClientFields)`, which adds `id`, `archived`, `archived_at`, `created_at` and `updated_at`;
  - `ClientPatch`, where every field is optional, plus `archived: Optional[bool]`;
  - `Store(path)`, with `create_client(fields) -> Client`, `get_client(id) -> Client`, `list_clients(archived=False) -> list[Client]` and `update_client(id, changes: dict) -> Client`;
  - `store._now() -> str`, which tests patch.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
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
```

Append to `tests/test_config.py`, adding `from pathlib import Path` and `from ledgersync import config` to its imports if they are missing:

```python
def test_the_database_lives_in_the_projects_data_folder_unless_set_otherwise(tmp_path):
    project = Path(config.__file__).resolve().parent.parent
    assert Settings.from_env({}).db_path == project / "data" / "ledgersync.db"
    assert Settings.from_env({"LEDGERSYNC_DB_PATH": str(tmp_path / "books.db")}).db_path == tmp_path / "books.db"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_store.py tests/test_config.py`
Expected: FAIL. `test_store.py` fails at collection with `ImportError: cannot import name 'ClientArchived'`. The config test fails with `AttributeError: 'Settings' object has no attribute 'db_path'`.

- [ ] **Step 3: The setting, the errors and the client models**

In `ledgersync/config.py`, under `ENV_FILE = ...`, add:

```python
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "ledgersync.db"   # git-ignored: data/
```

In `Settings`, after `business_name`, add:

```python
    db_path: Path = DEFAULT_DB            # the local database of clients and their saved rows
```

At the end of `from_env`'s `cls(...)` call, after `business_name=...`, add:

```python
            db_path=(Path(env["LEDGERSYNC_DB_PATH"].strip()).expanduser()
                     if env.get("LEDGERSYNC_DB_PATH", "").strip() else d.db_path),
```

Append to `ledgersync/errors.py`:

```python
class NotFound(LedgerSyncError):
    status_code = 404
    code = "not_found"


class ClientArchived(LedgerSyncError):
    """A change to an archived client: it can only be restored."""

    status_code = 409
    code = "client_archived"


class InvalidInput(LedgerSyncError):
    status_code = 422
    code = "invalid_input"


class StorageError(LedgerSyncError):
    """The local database could not be read or written; the server log says why."""

    status_code = 500
    code = "storage_error"
```

Append to `ledgersync/models.py`:

```python
class ClientFields(BaseModel):
    """What a person enters for a client, trimmed; ledger.check_client says what is missing."""
    name: str
    business_type: BusinessType
    contact_name: str                  # the responsible person: the client's own contact
    contact_email: str = ""
    contact_phone: str = ""
    vat_registered: bool = True

    @field_validator("name", "contact_name", "contact_email", "contact_phone", mode="before")
    @classmethod
    def _trimmed(cls, value):
        return "" if value is None else str(value).strip()


class Client(ClientFields):
    id: int
    archived: bool = False
    archived_at: Optional[str] = None
    created_at: str
    updated_at: str


class ClientPatch(BaseModel):
    """A change to a client: only the fields sent change; archived true archives it, false restores it."""
    name: Optional[str] = None
    business_type: Optional[BusinessType] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    vat_registered: Optional[bool] = None
    archived: Optional[bool] = None
```

Append to `.gitignore`:

```
# The local database of clients and their saved rows (LEDGERSYNC_DB_PATH)
data/
```

- [ ] **Step 4: The store, with clients**

Create `ledgersync/store.py`:

```python
"""The local database (design of 2026-10-06): clients, their uploads, and each upload's rows, in one SQLite
file through the standard library. One connection per call, so background jobs and requests use it side by
side. Rows keep their inputs only; statuses, matching and warnings are worked out again on every read."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ClientArchived, NotFound, StorageError
from .models import Client, ClientFields

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    contact_email TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    vat_registered INTEGER NOT NULL DEFAULT 1,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rows_by_client ON rows(client_id, upload_id, position);
"""
_CLIENT_FIELDS = ("name", "business_type", "contact_name", "contact_email", "contact_phone", "vat_registered")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._ready = False
        self._lock = threading.Lock()

    # ─── Clients ───────────────────────────────────────────────────────────────

    def create_client(self, fields: ClientFields) -> Client:
        now = _now()
        with self._db(write=True) as db:
            cursor = db.execute(
                "INSERT INTO clients (name, business_type, contact_name, contact_email, contact_phone,"
                " vat_registered, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fields.name, fields.business_type, fields.contact_name, fields.contact_email, fields.contact_phone,
                 int(fields.vat_registered), now, now))
            return _client(db, cursor.lastrowid)

    def get_client(self, client_id: int) -> Client:
        with self._db() as db:
            return _client(db, client_id)

    def list_clients(self, archived: bool = False) -> list[Client]:
        """The active clients, or the archived ones, by name."""
        with self._db() as db:
            found = db.execute("SELECT * FROM clients WHERE (archived_at IS NOT NULL) = ? "
                               "ORDER BY name COLLATE NOCASE, id", (int(archived),)).fetchall()
        return [_client_from(row) for row in found]

    def update_client(self, client_id: int, changes: dict) -> Client:
        """Changes the fields given; `archived` True archives the client and False restores it. An archived
        client can only be restored."""
        with self._db(write=True) as db:
            client = _client(db, client_id)
            fields = {k: changes[k] for k in _CLIENT_FIELDS if changes.get(k) is not None}
            if client.archived and fields:
                raise ClientArchived(f"Restore {client.name} to change it.")
            now = _now()
            sets = {k: int(v) if isinstance(v, bool) else v for k, v in fields.items()}
            archived = changes.get("archived")
            if archived is not None and archived != client.archived:
                sets["archived_at"] = now if archived else None
            if sets:
                sets["updated_at"] = now
                assignments = ", ".join(f"{k} = ?" for k in sets)
                db.execute(f"UPDATE clients SET {assignments} WHERE id = ?", (*sets.values(), client_id))
            return _client(db, client_id)

    # ─── The database ──────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        """Creates the folder, the file and the tables on first use, so starting the API touches nothing."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as db:
                    db.execute("PRAGMA journal_mode = WAL")
                    db.executescript(_SCHEMA)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except (OSError, sqlite3.Error) as exc:
                raise self._failed(exc) from None
            self._ready = True

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """One connection for one call, in one transaction. A write takes the write lock up front (waiting up
        to ten seconds for another writer), so a background job and a person never trip over each other."""
        self._prepare()
        try:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        except sqlite3.Error as exc:
            raise self._failed(exc) from None
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(db)
            raise self._failed(exc) from None
        except BaseException:
            _rollback(db)
            raise
        finally:
            db.close()

    def _failed(self, exc: Exception) -> StorageError:
        logger.error("The database at %s could not be used: %s", self.path, exc)
        return StorageError("The local database could not be read or written. Details are in the server log.")


def _rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("ROLLBACK")


def _client(db: sqlite3.Connection, client_id: int) -> Client:
    row = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        raise NotFound("This client was not found.")
    return _client_from(row)


def _client_from(row: sqlite3.Row) -> Client:
    return Client(id=row["id"], name=row["name"], business_type=row["business_type"],
                  contact_name=row["contact_name"], contact_email=row["contact_email"],
                  contact_phone=row["contact_phone"], vat_registered=bool(row["vat_registered"]),
                  archived=row["archived_at"] is not None, archived_at=row["archived_at"],
                  created_at=row["created_at"], updated_at=row["updated_at"])
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_store.py tests/test_config.py`
Expected: PASS.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures. Run `git status --short` and confirm the suite created no `data/` folder in the project: nothing opens the database at import.

- [ ] **Step 6: Commit**

```bash
git add .gitignore ledgersync/config.py ledgersync/errors.py ledgersync/models.py ledgersync/store.py tests/test_store.py tests/test_config.py
git commit -m "Store: a local SQLite database of clients, created on first use

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The store — uploads and rows, edits and revert

**Files:**
- Modify: `ledgersync/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes:
  - from Task 2, `checks.is_derived`;
  - from Task 3, the `Transaction` balance inputs;
  - from Task 5, `Store`, `_now` and `_client`.
- Produces:
  - `INPUTS`, `EDITABLE` and `WHOLE_DOCUMENT`;
  - `StoredRow(id, upload_id, tx: Transaction, original: Optional[dict])`;
  - `saved_form(tx) -> dict`;
  - `Store.add_upload(client_id, name, kind, rows, *, sha256="", model="", warnings=()) -> int`;
  - `Store.uploads(client_id) -> list[dict]`. Each dict has the keys `id`, `client_id`, `name`, `kind`, `sha256`, `model`, `warnings` (a list), `created_at` and `row_count`. The list is newest first.
  - `Store.delete_upload(client_id, upload_id) -> None`;
  - `Store.rows(client_id) -> list[StoredRow]`, in table order: by upload, oldest first, then by position;
  - `Store.patch_row(client_id, row_id, changes: dict, *, revert=False) -> None`. `changes` holds JSON values.

- [ ] **Step 1: Write the failing tests**

Add to the imports of `tests/test_store.py`:

```python
import datetime as dt
import threading
from decimal import Decimal

from ledgersync.checks import issue
from ledgersync.models import Transaction
```

Then append:

```python
def bill(**changes) -> Transaction:
    fields = dict(direction="out", gross="72.00", account_code="7502", date=dt.date(2026, 9, 1),
                  description="BT Business, broadband", document_type="invoice", counterparty="BT Business")
    return Transaction(**{**fields, **changes})


def test_an_upload_keeps_its_rows_in_order_and_only_their_inputs(store):
    client = store.create_client(CUBE)
    worked_out = bill(document_ref="bt", vat_posted="12.00", net="60.00", owed="72.00",
                      issues=[issue("total_mismatch", "The rows add up to £82.00."), issue("not_booked", "x", "info")])
    upload_id = store.add_upload(client.id, "BT-0905.pdf", "pdf", [worked_out, bill(document_ref="bt", gross="10.00")],
                                 sha256="ab12", model="fake-model", warnings=["Row 3 skipped"])
    first, second = store.rows(client.id)
    assert (first.upload_id, first.tx.gross, second.tx.gross) == (upload_id, Decimal("72.00"), Decimal("10.00"))
    assert (first.tx.vat_posted, first.tx.net, first.tx.owed) == (None, None, None)   # worked out on every read
    assert [i.code for i in first.tx.issues] == ["total_mismatch"]                      # the adapter's own check
    [upload] = store.uploads(client.id)
    assert (upload["name"], upload["kind"], upload["sha256"], upload["model"], upload["warnings"],
            upload["row_count"]) == ("BT-0905.pdf", "pdf", "ab12", "fake-model", ["Row 3 skipped"], 2)


def test_a_row_saved_without_a_document_reference_gets_one(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "Manual entry", "manual", [bill(document_ref=None)])
    assert store.rows(client.id)[0].tx.document_ref


def test_uploads_are_listed_newest_first(store):
    client = store.create_client(CUBE)
    older = store.add_upload(client.id, "a.pdf", "pdf", [bill()])
    newer = store.add_upload(client.id, "b.pdf", "pdf", [bill()])
    assert [u["id"] for u in store.uploads(client.id)] == [newer, older]


def test_removing_an_upload_removes_its_rows_and_nothing_else(store):
    client = store.create_client(CUBE)
    keep = store.add_upload(client.id, "a.pdf", "pdf", [bill()])
    gone = store.add_upload(client.id, "b.pdf", "pdf", [bill(), bill()])
    store.delete_upload(client.id, gone)
    assert [r.upload_id for r in store.rows(client.id)] == [keep]
    with pytest.raises(NotFound):
        store.delete_upload(client.id, gone)


def test_another_clients_rows_and_uploads_are_not_found(store):
    mine, theirs = store.create_client(CUBE), store.create_client(CUBE)
    upload = store.add_upload(theirs.id, "b.pdf", "pdf", [bill()])
    row = store.rows(theirs.id)[0].id
    with pytest.raises(NotFound):
        store.delete_upload(mine.id, upload)
    with pytest.raises(NotFound):
        store.patch_row(mine.id, row, {"include": True})


def test_include_and_document_wide_fields_change_every_row_of_the_document(store):
    client = store.create_client(CUBE)
    quote = dict(document_type="quote", document_ref="q1")
    store.add_upload(client.id, "q.pdf", "pdf", [bill(**quote), bill(**quote, gross="5.00"), bill(document_ref="other")])
    first = store.rows(client.id)[0].id
    store.patch_row(client.id, first, {"include": True, "counterparty": "BT plc", "gross": "70.00"})
    assert [(r.tx.include, r.tx.counterparty, r.tx.gross) for r in store.rows(client.id)] == [
        (True, "BT plc", Decimal("70.00")), (True, "BT plc", Decimal("5.00")), (False, "BT Business", Decimal("72.00"))]


def test_a_rows_first_edit_keeps_what_it_held_and_revert_brings_the_document_back(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [bill(document_ref="a"), bill(document_ref="a", gross="10.00")])
    first, second = (r.id for r in store.rows(client.id))
    store.patch_row(client.id, first, {"gross": "70.00"})
    store.patch_row(client.id, first, {"account_code": "7504"})
    store.patch_row(client.id, second, {"document_type": "receipt"})   # a whole-document field
    one, two = store.rows(client.id)
    assert (one.tx.gross, one.tx.account_code, one.tx.document_type, two.tx.document_type) == (
        Decimal("70.00"), "7504", "receipt", "receipt")
    assert (one.original["gross"], one.original["account_code"], one.original["document_type"]) == (
        "72.00", "7502", "invoice")
    store.patch_row(client.id, second, {}, revert=True)
    one, two = store.rows(client.id)
    assert (one.tx.gross, one.tx.account_code, one.tx.document_type, one.original) == (
        Decimal("72.00"), "7502", "invoice", None)
    assert (two.tx.document_type, two.original) == ("invoice", None)


def test_a_link_is_not_an_edit(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "s.csv", "table", [bill(document_type="statement")])
    row = store.rows(client.id)[0].id
    store.patch_row(client.id, row, {"link": []})
    [saved] = store.rows(client.id)
    assert (saved.tx.link, saved.original) == ([], None)


def test_saving_rows_moves_the_clients_updated_time(store, monkeypatch):
    client = store.create_client(CUBE)
    monkeypatch.setattr("ledgersync.store._now", lambda: "2030-01-01T00:00:00+00:00")
    store.add_upload(client.id, "a.pdf", "pdf", [bill()])
    assert store.get_client(client.id).updated_at == "2030-01-01T00:00:00+00:00"


def test_jobs_and_people_can_write_at_the_same_time(store):
    # Review focus: a background job saves uploads while a person links a row; neither may fail.
    client = store.create_client(CUBE)
    store.add_upload(client.id, "s.csv", "table", [bill(document_type="statement")])
    row = store.rows(client.id)[0].id
    failures = []

    def save():
        try:
            for _ in range(15):
                store.add_upload(client.id, "s.csv", "table", [bill()])
        except Exception as exc:   # the assertion below reports it
            failures.append(exc)

    def link():
        try:
            for n in range(15):
                store.patch_row(client.id, row, {"link": [] if n % 2 else None})
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=save) for _ in range(3)] + [threading.Thread(target=link)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == [] and len(store.rows(client.id)) == 46
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_store.py`
Expected: the new tests FAIL with `AttributeError: 'Store' object has no attribute 'add_upload'`.

- [ ] **Step 3: Uploads and rows**

In `ledgersync/store.py`:

1. Add `import json` and `import uuid` to the imports.
2. Change `from typing import Iterator` to `from typing import Iterator, NamedTuple, Optional`.
3. Add `from .checks import is_derived`.
4. Change the models import to `from .models import Client, ClientFields, Transaction`.

After `_CLIENT_FIELDS`, add:

```python
# What is kept of a row: the inputs a document or a person gave. Everything else is worked out on read.
INPUTS = {"date", "description", "direction", "gross", "vat", "vat_treatment", "account_code", "contra_account_code",
          "currency", "source", "method", "evidence", "document_type", "counterparty", "document_ref", "include",
          "link", "balance", "opening_balance", "closing_balance"}
# The fields a person can edit; a row's first edit keeps what they held as `original`.
EDITABLE = ("date", "description", "counterparty", "direction", "gross", "vat", "account_code", "document_type")
# Fields of a whole document: a change to one row changes every row with its document_ref.
WHOLE_DOCUMENT = {"counterparty", "document_type", "include"}


class StoredRow(NamedTuple):
    id: int
    upload_id: int
    tx: Transaction
    original: Optional[dict]   # what the editable fields held before a person's first edit; None if never edited


def saved_form(tx: Transaction) -> dict:
    """A row as the database keeps it: its inputs, the issues the ledger doesn't work out itself, and a
    document reference (a manual entry arrives without one)."""
    data = tx.model_dump(mode="json", include=INPUTS)
    data["issues"] = [found.model_dump() for found in tx.issues if not is_derived(found)]
    data["document_ref"] = data.get("document_ref") or uuid.uuid4().hex[:12]
    return data
```

In `Store`, after `update_client`, add:

```python
    # ─── Uploads and rows ──────────────────────────────────────────────────────

    def add_upload(self, client_id: int, name: str, kind: str, rows: list[Transaction], *, sha256: str = "",
                   model: str = "", warnings=()) -> int:
        """Saves an upload and its rows in order, and returns the upload's id. Archived or not: a job that
        finishes after its client was archived is still kept."""
        now = _now()
        with self._db(write=True) as db:
            _client(db, client_id)
            upload_id = db.execute(
                "INSERT INTO uploads (client_id, name, kind, sha256, model, warnings, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_id, name, kind, sha256, model or "", json.dumps(list(warnings)), now)).lastrowid
            db.executemany("INSERT INTO rows (client_id, upload_id, position, data) VALUES (?, ?, ?, ?)",
                           [(client_id, upload_id, n, json.dumps(saved_form(tx))) for n, tx in enumerate(rows)])
            _touch(db, client_id, now)
            return upload_id

    def uploads(self, client_id: int) -> list[dict]:
        """The client's uploads, newest first, each with its row count."""
        with self._db() as db:
            _client(db, client_id)
            found = db.execute("SELECT u.*, (SELECT COUNT(*) FROM rows r WHERE r.upload_id = u.id) AS row_count"
                               " FROM uploads u WHERE u.client_id = ? ORDER BY u.id DESC", (client_id,)).fetchall()
        return [{**dict(row), "warnings": json.loads(row["warnings"])} for row in found]

    def delete_upload(self, client_id: int, upload_id: int) -> None:
        """Removes an upload and its rows for good."""
        with self._db(write=True) as db:
            if not db.execute("DELETE FROM uploads WHERE id = ? AND client_id = ?", (upload_id, client_id)).rowcount:
                raise NotFound("This upload was not found.")
            _touch(db, client_id, _now())

    def rows(self, client_id: int) -> list[StoredRow]:
        """The client's rows in table order: by upload, oldest first, then as the model gave them."""
        with self._db() as db:
            _client(db, client_id)
            found = db.execute("SELECT id, upload_id, data FROM rows WHERE client_id = ? ORDER BY upload_id, position",
                               (client_id,)).fetchall()
        return [_stored(row) for row in found]

    def patch_row(self, client_id: int, row_id: int, changes: dict, *, revert: bool = False) -> None:
        """A person's change to a row: Link, Include, an edit, or revert. Include, counterparty and document
        type change every row of its document (the same document_ref); the rest change that row. A row's first
        edit keeps what it held as `original`; revert puts back every edited row of the document."""
        with self._db(write=True) as db:
            saved = {row["id"]: json.loads(row["data"]) for row in
                     db.execute("SELECT id, data FROM rows WHERE client_id = ?", (client_id,))}
            if row_id not in saved:
                raise NotFound("This row was not found.")
            ref = saved[row_id].get("document_ref")
            for rid, data in saved.items():
                if rid != row_id and not (ref and data.get("document_ref") == ref):
                    continue
                if revert:
                    if "original" not in data:
                        continue
                    data.update(data.pop("original"))
                else:
                    change = changes if rid == row_id else {k: v for k, v in changes.items() if k in WHOLE_DOCUMENT}
                    if not change:
                        continue
                    if "original" not in data and any(k in EDITABLE and data.get(k) != v for k, v in change.items()):
                        data["original"] = {k: data.get(k) for k in EDITABLE}
                    data.update(change)
                db.execute("UPDATE rows SET data = ? WHERE id = ?", (json.dumps(data), rid))
            _touch(db, client_id, _now())
```

After `_rollback`, add:

```python
def _touch(db: sqlite3.Connection, client_id: int, now: str) -> None:
    db.execute("UPDATE clients SET updated_at = ? WHERE id = ?", (now, client_id))


def _stored(row: sqlite3.Row) -> StoredRow:
    data = json.loads(row["data"])
    original = data.pop("original", None)
    return StoredRow(row["id"], row["upload_id"], Transaction.model_validate(data), original)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_store.py`
Expected: PASS, including `test_jobs_and_people_can_write_at_the_same_time`.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add ledgersync/store.py tests/test_store.py
git commit -m "Store: uploads and their rows as inputs only, edits with the original kept, and revert

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: A client's ledger and a person's changes

**Files:**
- Modify: `ledgersync/models.py` (`ClientSummary`, `Upload`, `SavedRow`, `Ledger`, `RowPatch`, `ManualUpload`)
- Create: `ledgersync/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes:
  - from Task 4, `check_statement` and `StatementCheck`;
  - from Tasks 5 and 6, `Store` and `StoredRow`;
  - from Task 1, `choosable`.
- Produces, in `ledgersync/ledger.py`:
  - `settings_for(client) -> BusinessSettings`;
  - `require_active(client)`, which raises `ClientArchived`;
  - `check_client(fields)`, which raises `InvalidInput` with "Enter the company name.", "Enter the responsible person." or "Enter a valid email.";
  - `add_client(store, fields) -> Client`;
  - `change_client(store, client_id, patch: ClientPatch) -> Client`;
  - `build(store, client_id) -> Ledger`;
  - `summaries(store, archived=False) -> list[ClientSummary]`;
  - `change_row(store, client_id, row_id, patch: RowPatch) -> Ledger`;
  - `add_manual(store, client_id, upload: ManualUpload) -> Ledger`;
  - `remove_upload(store, client_id, upload_id) -> Ledger`;
  - `trial_balance(store, client_id) -> TrialBalance`.

  The models are the ones listed under Files. `SavedRow` is a `Transaction` plus `id`, `upload_id`, `edited` and `original`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ledger.py`:

```python
import datetime as dt
import re
from decimal import Decimal

import pytest

from ledgersync import ledger
from ledgersync.errors import ClientArchived, InvalidInput
from ledgersync.models import ClientFields, ClientPatch, ManualUpload, RowPatch, Transaction
from ledgersync.store import Store

CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ledgersync.db")


def row(**changes) -> Transaction:
    fields = dict(direction="out", gross="72.00", account_code="7502", date=dt.date(2026, 9, 1), description="BT",
                  document_type="invoice", counterparty="BT Business")
    return Transaction(**{**fields, **changes})


def test_a_bill_and_its_payment_saved_apart_are_paired_in_the_ledger(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "BT-0905.pdf", "pdf", [row(document_ref="bt")])
    store.add_upload(client.id, "Barclays.csv", "table", [row(date=dt.date(2026, 9, 5), document_type="statement",
                                                              counterparty="BT BUSINESS DD", document_ref="l1")])
    built = ledger.build(store, client.id)
    bill, line = built.transactions
    assert ([s.ref for s in line.pays], line.paid_against, bill.owed) == (["bt"], "2100", Decimal("0.00"))
    assert [u.name for u in built.uploads] == ["Barclays.csv", "BT-0905.pdf"]


def test_the_clients_vat_setting_and_kind_of_business_are_applied(store):
    client = store.create_client(CUBE.model_copy(update={"vat_registered": False}))
    store.add_upload(client.id, "a.pdf", "pdf", [row(vat="12.00"), row(account_code="3260", document_type="receipt")])
    bill, drawings = ledger.build(store, client.id).transactions
    assert (bill.vat_posted, bill.net) == (Decimal("0.00"), Decimal("72.00"))
    assert "director_loan" in [i.code for i in drawings.issues]


def test_each_statement_upload_gets_its_balance_check(store):
    client = store.create_client(CUBE)
    line = dict(document_type="statement", counterparty="Shop")
    statement = store.add_upload(client.id, "Barclays.csv", "table", [
        row(**line, gross="20.00", balance="980.00", document_ref="l1"),
        row(**line, gross="30.00", balance="900.00", document_ref="l2")])
    receipt = store.add_upload(client.id, "r.jpg", "image", [row(document_type="receipt")])
    checks = {u.id: u.statement for u in ledger.build(store, client.id).uploads}
    assert (checks[statement].status, checks[statement].difference, checks[receipt]) == ("gap", Decimal("50.00"), None)


def test_summaries_count_rows_and_what_needs_review(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row(), row(account_code="9999", document_ref="x")])
    [summary] = ledger.summaries(store)
    assert (summary.id, summary.rows, summary.to_review) == (client.id, 2, 1)


def test_an_edit_is_checked_before_it_is_saved(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    for patch, message in ((RowPatch(gross="0"), "The amount must be above zero."),
                           (RowPatch(vat="72.00"), "VAT must be below the amount."),
                           (RowPatch(description=" "), "Enter a description."),
                           (RowPatch(account_code="3260"), "Account 3260 can't be chosen for Business Cube Ltd."),
                           (RowPatch(link=[]), "Only a bank line can be linked to documents.")):
        with pytest.raises(InvalidInput, match=re.escape(message)):
            ledger.change_row(store, client.id, rid, patch)
    assert store.rows(client.id)[0].original is None


def test_an_edit_rebooks_the_row_and_revert_brings_back_what_was_read(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    [edited] = ledger.change_row(store, client.id, rid, RowPatch(document_type="receipt", gross="70.00")).transactions
    assert (edited.edited, edited.owed, edited.contra_account_code, edited.original["gross"]) == (
        True, None, "1200", "72.00")
    [back] = ledger.change_row(store, client.id, rid, RowPatch(revert=True)).transactions
    assert (back.edited, back.gross, back.owed) == (False, Decimal("72.00"), Decimal("72.00"))


def test_a_bank_line_turned_into_an_invoice_becomes_owed(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "s.csv", "table", [row(document_type="statement", counterparty="Clearway")])
    rid = store.rows(client.id)[0].id
    [invoice] = ledger.change_row(store, client.id, rid, RowPatch(document_type="invoice")).transactions
    assert (bool(invoice.document_ref), invoice.owed, invoice.contra_account_code) == (True, Decimal("72.00"), "2100")


def test_fixing_a_misread_amount_clears_the_statement_gap(store):
    client = store.create_client(CUBE)
    line = dict(document_type="statement", counterparty="Shop")
    store.add_upload(client.id, "s.csv", "table", [row(**line, gross="20.00", balance="980.00", document_ref="l1"),
                                                   row(**line, gross="30.00", balance="900.00", document_ref="l2")])
    second = store.rows(client.id)[1].id
    built = ledger.change_row(store, client.id, second, RowPatch(gross="80.00"))
    assert (built.uploads[0].statement.status, built.transactions[1].issues) == ("ok", [])


def test_an_archived_clients_rows_cannot_change(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    store.update_client(client.id, {"archived": True})
    with pytest.raises(ClientArchived):
        ledger.change_row(store, client.id, rid, RowPatch(include=True))


def test_a_client_needs_a_name_a_responsible_person_and_a_real_email(store):
    for change, message in (({"name": ""}, "Enter the company name."),
                            ({"contact_name": ""}, "Enter the responsible person."),
                            ({"contact_email": "jenny"}, "Enter a valid email.")):
        with pytest.raises(InvalidInput, match=re.escape(message)):
            ledger.add_client(store, CUBE.model_copy(update=change))
    client = ledger.add_client(store, CUBE)
    with pytest.raises(InvalidInput, match="Enter the company name"):
        ledger.change_client(store, client.id, ClientPatch(name="  "))
    assert ledger.change_client(store, client.id, ClientPatch(contact_phone=" 07700 900123 ")).contact_phone == "07700 900123"


def test_a_manual_entry_is_saved_as_its_own_upload(store):
    client = store.create_client(CUBE)
    built = ledger.add_manual(store, client.id, ManualUpload(transactions=[row(document_type="receipt", method="user")]))
    assert [(u.name, u.kind, u.rows) for u in built.uploads] == [("Manual entry", "manual", 1)]
    with pytest.raises(InvalidInput):
        ledger.add_manual(store, client.id, ManualUpload(transactions=[]))


def test_the_trial_balance_comes_from_the_saved_rows(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row(document_type="receipt", vat="12.00")])
    assert [(l.code, l.debit, l.credit) for l in ledger.trial_balance(store, client.id).lines] == [
        ("1200", Decimal("0.00"), Decimal("72.00")), ("2201", Decimal("12.00"), Decimal("0.00")),
        ("7502", Decimal("60.00"), Decimal("0.00"))]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_ledger.py`
Expected: FAIL at collection with `ImportError: cannot import name 'ManualUpload'`.

- [ ] **Step 3: The ledger's models**

Append to `ledgersync/models.py`:

```python
class ClientSummary(Client):
    rows: int = 0
    to_review: int = 0   # rows with an error or a warning


class Upload(BaseModel):
    id: int
    name: str
    kind: str
    sha256: str = ""
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: str
    rows: int = 0
    statement: Optional[StatementCheck] = None   # a bank statement's balance check; None for other uploads


class SavedRow(Transaction):
    """A saved row as the pages show it: its id, its upload, and what was read before a person's first edit."""
    id: int
    upload_id: int
    edited: bool = False
    original: Optional[dict] = None


class Ledger(BaseModel):
    client: Client
    uploads: list[Upload]
    transactions: list[SavedRow]


class RowPatch(BaseModel):
    """A person's change to a saved row: Link or Include, an edit of what was read, or revert. Only the fields
    sent change; ledger.change_row checks them."""
    link: Optional[list[str]] = None
    include: Optional[bool] = None
    date: Optional[dt.date] = None
    description: Optional[str] = None
    counterparty: Optional[str] = None
    direction: Optional[Direction] = None
    gross: Optional[Decimal] = None
    vat: Optional[Decimal] = None
    account_code: Optional[str] = None
    document_type: Optional[DocumentType] = None
    revert: bool = False

    @field_validator("gross", "vat", mode="before")
    @classmethod
    def _pennies(cls, value):
        if value is None or value == "":
            return None
        money = to_money(value)
        if money is None:
            raise ValueError("not an amount")
        return money

    @field_validator("description", "counterparty", mode="before")
    @classmethod
    def _trimmed(cls, value):
        return None if value is None else (str(value).strip() or None)


class ManualUpload(BaseModel):
    name: str = "Manual entry"
    kind: Literal["manual"] = "manual"
    transactions: list[Transaction]
```

- [ ] **Step 4: The ledger**

Create `ledgersync/ledger.py`:

```python
"""A client's ledger as the pages show it (design of 2026-10-06): the saved rows booked and matched with the
client's own settings, each bank statement checked against its balances; the client summaries; and a
person's changes, checked before the store saves them."""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .accounts import choosable
from .checks import normalise
from .errors import ClientArchived, InvalidInput, NotFound
from .matching import match
from .models import (BusinessSettings, Client, ClientFields, ClientPatch, ClientSummary, Ledger, ManualUpload,
                     RowPatch, SavedRow, StatementCheck, Transaction, TrialBalance, Upload)
from .posting import trial_balance as post
from .statements import check_statement
from .store import Store


def settings_for(client: Client) -> BusinessSettings:
    """How a client's rows are booked: its name (whose books), its VAT registration and kind of business."""
    return BusinessSettings(business_name=client.name, vat_registered=client.vat_registered,
                            business_type=client.business_type)


def require_active(client: Client) -> None:
    if client.archived:
        raise ClientArchived(f"Restore {client.name} to add documents or make changes.")


def check_client(fields: ClientFields) -> None:
    """A client needs a company name and a responsible person, and an email needs an @."""
    if not fields.name:
        raise InvalidInput("Enter the company name.")
    if not fields.contact_name:
        raise InvalidInput("Enter the responsible person.")
    if fields.contact_email and "@" not in fields.contact_email:
        raise InvalidInput("Enter a valid email.")


def add_client(store: Store, fields: ClientFields) -> Client:
    check_client(fields)
    return store.create_client(fields)


def change_client(store: Store, client_id: int, patch: ClientPatch) -> Client:
    """Edits, archives or restores a client; an edited client must still be complete."""
    client = store.get_client(client_id)
    sent = patch.model_dump(exclude_unset=True)
    changes = {k: v for k, v in sent.items() if k != "archived" and v is not None}
    if changes and not client.archived:
        merged = ClientFields(**{**client.model_dump(include=set(ClientFields.model_fields)), **changes})
        check_client(merged)
        changes = {k: getattr(merged, k) for k in changes}
    if sent.get("archived") is not None:
        changes["archived"] = sent["archived"]
    return store.update_client(client_id, changes)


def needs_review(row: Transaction) -> bool:
    return any(found.severity in ("error", "warning") for found in row.issues)


def build(store: Store, client_id: int) -> Ledger:
    """The client's ledger: every saved row booked and matched with the client's settings, then each upload's
    bank lines checked against the balances its statement prints."""
    client = store.get_client(client_id)
    stored = store.rows(client_id)
    settings = settings_for(client)
    ready = match([normalise(row.tx, settings) for row in stored], settings)
    positions: dict[int, list[int]] = defaultdict(list)
    for n, row in enumerate(stored):
        positions[row.upload_id].append(n)
    checks: dict[int, Optional[StatementCheck]] = {}
    for upload_id, ns in positions.items():
        checked, checks[upload_id] = check_statement([ready[n] for n in ns])
        for n, tx in zip(ns, checked):
            ready[n] = tx
    transactions = [SavedRow(**tx.model_dump(), id=row.id, upload_id=row.upload_id,
                             edited=row.original is not None, original=row.original)
                    for row, tx in zip(stored, ready)]
    uploads = [Upload(id=u["id"], name=u["name"], kind=u["kind"], sha256=u["sha256"], model=u["model"],
                      warnings=u["warnings"], created_at=u["created_at"], rows=u["row_count"],
                      statement=checks.get(u["id"])) for u in store.uploads(client_id)]
    return Ledger(client=client, uploads=uploads, transactions=transactions)


def summaries(store: Store, archived: bool = False) -> list[ClientSummary]:
    """The clients page: each client with how many rows it has and how many need a look."""
    found = []
    for client in store.list_clients(archived):
        rows = build(store, client.id).transactions
        found.append(ClientSummary(**client.model_dump(), rows=len(rows),
                                   to_review=sum(needs_review(r) for r in rows)))
    return found


# A field sent as null that can't be left empty, and what to tell the person.
_NEEDED = {"direction": "Choose money in or out.", "gross": "Enter the amount.", "account_code": "Choose an account.",
           "document_type": "Choose a document type.", "include": "Tick or untick Include."}


def _check_edit(patch: RowPatch, sent: set[str], tx: Transaction, client: Client) -> None:
    for field, message in _NEEDED.items():
        if field in sent and getattr(patch, field) is None:
            raise InvalidInput(message)
    if "description" in sent and not patch.description:
        raise InvalidInput("Enter a description.")
    if "link" in sent and (patch.document_type if "document_type" in sent else tx.document_type) != "statement":
        raise InvalidInput("Only a bank line can be linked to documents.")
    if "account_code" in sent and patch.account_code not in {a.code for a in choosable(client.business_type)}:
        raise InvalidInput(f"Account {patch.account_code} can't be chosen for {client.name}.")
    gross = patch.gross if "gross" in sent else tx.gross
    vat = patch.vat if "vat" in sent else tx.vat
    if gross <= 0:
        raise InvalidInput("The amount must be above zero.")
    if vat is not None and vat < 0:
        raise InvalidInput("VAT can't be negative.")
    if vat is not None and vat >= gross:
        raise InvalidInput("VAT must be below the amount.")


def change_row(store: Store, client_id: int, row_id: int, patch: RowPatch) -> Ledger:
    """Saves a person's change to a row once it makes sense, and returns the ledger worked out again."""
    client = store.get_client(client_id)
    require_active(client)
    row = next((r for r in store.rows(client_id) if r.id == row_id), None)
    if row is None:
        raise NotFound("This row was not found.")
    if patch.revert:
        store.patch_row(client_id, row_id, {}, revert=True)
    else:
        sent = patch.model_fields_set - {"revert"}
        _check_edit(patch, sent, row.tx, client)
        store.patch_row(client_id, row_id, patch.model_dump(mode="json", include=sent))
    return build(store, client_id)


def add_manual(store: Store, client_id: int, upload: ManualUpload) -> Ledger:
    client = store.get_client(client_id)
    require_active(client)
    if not upload.transactions:
        raise InvalidInput("Add at least one row.")
    store.add_upload(client_id, upload.name.strip() or "Manual entry", upload.kind, upload.transactions)
    return build(store, client_id)


def remove_upload(store: Store, client_id: int, upload_id: int) -> Ledger:
    require_active(store.get_client(client_id))
    store.delete_upload(client_id, upload_id)
    return build(store, client_id)


def trial_balance(store: Store, client_id: int) -> TrialBalance:
    """The client's trial balance from its saved rows; InvalidTransactions (422) while rows need fixing."""
    client = store.get_client(client_id)
    return post([row.tx for row in store.rows(client_id)], settings_for(client))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_ledger.py`
Expected: PASS.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 6: Commit**

```bash
git add ledgersync/models.py ledgersync/ledger.py tests/test_ledger.py
git commit -m "Ledger: a client's saved rows booked with its settings, statements checked, and checked edits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The client API

**Files:**
- Modify: `server.py`:
  - `create_app(..., store=None)`;
  - CORS allows `PATCH`;
  - the `/api/accounts` query;
  - the client endpoints.
- Test: `tests/test_clients_api.py`

**Interfaces:**
- Consumes: from Task 7, everything in `ledgersync.ledger`; from Tasks 5 and 6, `Store`.
- Produces these endpoints:
  - `GET /api/clients?archived=false|true`, which returns `list[ClientSummary]`;
  - `POST /api/clients`, which returns 201 and a `Client`;
  - `GET /api/clients/{id}` and `PATCH /api/clients/{id}`, which return a `Client`;
  - `GET /api/clients/{id}/ledger`, which returns a `Ledger`;
  - `PATCH /api/clients/{id}/rows/{row_id}`, which returns a `Ledger`;
  - `POST /api/clients/{id}/uploads`, which returns 201 and a `Ledger`;
  - `DELETE /api/clients/{id}/uploads/{upload_id}`, which returns a `Ledger`;
  - `GET /api/clients/{id}/trial-balance`, which returns a `TrialBalance`;
  - `GET /api/accounts?business_type=...`, which returns the accounts that kind of business can choose; without the query it returns the whole chart, as before.

  `create_app(settings=None, *, model_client=None, store=None)` defaults the store to `Store(settings.db_path)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clients_api.py`:

```python
import pytest
from fakes import FakeModel
from fastapi.testclient import TestClient

from ledgersync.config import Settings
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
        client = TestClient(app)
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
    assert (bad.status_code, bad.json()["detail"]["message"]) == (422, "Only a bank line can be linked to documents.")
    [back, _] = client.patch(f"/api/clients/{cid}/rows/{bill_id}", json={"revert": True}).json()["transactions"]
    assert (back["gross"], back["edited"]) == ("72.00", False)


def test_removing_an_upload_leaves_a_stale_link(api):
    client = api()
    cid = add(client)["id"]
    bill_upload = manual(client, cid, BILL)["uploads"][0]["id"]
    manual(client, cid, {**LINE, "link": ["bt"]})
    [line] = client.delete(f"/api/clients/{cid}/uploads/{bill_upload}").json()["transactions"]
    assert "stale_link" in [i["code"] for i in line["issues"]]


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
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_clients_api.py`
Expected: FAIL with `TypeError: create_app() got an unexpected keyword argument 'store'`.

- [ ] **Step 3: The endpoints**

In `server.py`:

1. Change the package imports to:

```python
from ledgersync import accounts, adapter, intake, pipeline, posting
from ledgersync import ledger as books
from ledgersync.accounts import choosable
```

2. Change the models import to:

```python
from ledgersync.models import (AnalysisResult, BusinessSettings, BusinessType, Client, ClientFields, ClientPatch,
                               ClientSummary, Ledger, LedgerRequest, ManualUpload, RowPatch, TransactionList,
                               TrialBalance)
from ledgersync.store import Store
```

3. Change the signature and the start of `create_app` to:

```python
def create_app(settings: Optional[Settings] = None, *, model_client=None, store: Optional[Store] = None) -> FastAPI:
    """The API; tests pass a stand-in for the Groq client as model_client, and a store in a temporary folder."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    model_client = model_client or GroqClient(settings)
    store = store or Store(settings.db_path)   # opened on first use, not now
```

The lines from `extractor = ...` onwards stay as they are.

4. Change the CORS line to `allow_methods=["GET", "POST", "PATCH", "DELETE"]`.

5. Replace `list_accounts` with:

```python
    @app.get("/api/accounts")
    def list_accounts(business_type: Optional[BusinessType] = None):
        """The chart; with a business type, only the accounts a row of that business can be coded to."""
        chosen = choosable(business_type) if business_type else accounts.CHART
        return [{"code": a.code, "name": a.name, "type": a.type.value, "vat": a.vat.value} for a in chosen]
```

6. After `generate_trial_balance` and before `return app`, add:

```python
    # ─── Clients: saved work (design of 2026-10-06) ─────────────────────────────

    @app.get("/api/clients", response_model=list[ClientSummary])
    def list_clients(archived: bool = False):
        return books.summaries(store, archived)

    @app.post("/api/clients", response_model=Client, status_code=201)
    def add_client(fields: ClientFields):
        return books.add_client(store, fields)

    @app.get("/api/clients/{client_id}", response_model=Client)
    def get_client(client_id: int):
        return store.get_client(client_id)

    @app.patch("/api/clients/{client_id}", response_model=Client)
    def change_client(client_id: int, patch: ClientPatch):
        return books.change_client(store, client_id, patch)

    @app.get("/api/clients/{client_id}/ledger", response_model=Ledger)
    def client_ledger(client_id: int):
        return books.build(store, client_id)

    @app.patch("/api/clients/{client_id}/rows/{row_id}", response_model=Ledger)
    def change_row(client_id: int, row_id: int, patch: RowPatch):
        return books.change_row(store, client_id, row_id, patch)

    @app.post("/api/clients/{client_id}/uploads", response_model=Ledger, status_code=201)
    def add_manual_rows(client_id: int, upload: ManualUpload):
        return books.add_manual(store, client_id, upload)

    @app.delete("/api/clients/{client_id}/uploads/{upload_id}", response_model=Ledger)
    def remove_upload(client_id: int, upload_id: int):
        return books.remove_upload(store, client_id, upload_id)

    @app.get("/api/clients/{client_id}/trial-balance", response_model=TrialBalance)
    def client_trial_balance(client_id: int):
        return books.trial_balance(store, client_id)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_clients_api.py`
Expected: PASS.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures. `test_cors_allows_only_the_frontend` still passes.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_clients_api.py
git commit -m "API: clients, their saved ledgers, row changes, manual rows, uploads and trial balances

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Analysing for a client

**Files:**
- Modify: `server.py` (`run_analysis`, `analyze_transaction`)
- Test: `tests/test_clients_api.py`

**Interfaces:**
- Consumes:
  - from Task 3, `TransactionExtractor(..., business_type=...)`;
  - from Task 6, `Store.add_upload`;
  - from Task 7, `books.settings_for` and `books.require_active`.
- Produces: `POST /api/analyze` accepts an optional form field, `client_id`.
  - An unknown client gives 404 and an archived one 409, before anything is queued.
  - A succeeded job with rows saves an upload, named after the file or "Pasted text", with the file's SHA-256.
  - That job's `result` gains `client_id` and `upload_id`.

- [ ] **Step 1: Write the failing tests**

Add to the imports of `tests/test_clients_api.py`:

```python
import hashlib
import threading
import time

from fakes import ROW, transactions_json

from ledgersync.errors import ModelError
```

Then append:

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_clients_api.py`
Expected: the new tests FAIL, for these reasons:
- no `client_id` in the result (`KeyError`);
- no uploads saved;
- `200`/`202` instead of 404.

`test_analysing_without_a_client_saves_nothing` and `test_a_cancelled_job_saves_nothing` pass already, because nothing is saved yet. They guard the implementation.

- [ ] **Step 3: Read for the client and save what is found**

In `server.py`, add `import hashlib` at the top. Then replace `run_analysis` and `analyze_transaction` with:

```python
    def run_analysis(item, ctx, client: Optional[Client] = None, name: str = "Pasted text", sha256: str = "") -> dict:
        """Reads one input. For a client: the model is told its name and kind of business, the rows are booked
        with its settings, and they are saved to it as an upload when any were found (not after a cancel)."""
        if client is None:
            reader, rules = extractor, BusinessSettings(business_name=settings.business_name)
        else:
            rules = books.settings_for(client)
            reader = TransactionExtractor(model_client, business_name=client.name, business_type=client.business_type)
        extraction = pipeline.analyze(item, reader, ctx)
        transactions = adapter.to_transactions([row.model_dump() for row in extraction.data], source=item.kind,
                                               settings=rules)
        result = AnalysisResult(transactions=transactions, warnings=extraction.warnings,
                                model=extraction.model).model_dump(mode="json")
        if client is not None and transactions:
            ctx.check_cancelled()
            result["client_id"] = client.id
            result["upload_id"] = store.add_upload(client.id, name, item.kind, transactions, sha256=sha256,
                                                   model=extraction.model or "", warnings=extraction.warnings)
        return result

    @app.post("/api/analyze", status_code=202)
    def analyze_transaction(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
                            client_id: Optional[int] = Form(None)):
        """Validates the input now (404/409/413/415/422), then analyses it in a background job; with a
        client_id, the job saves the rows it finds to that client."""
        client = None
        if client_id is not None:
            client = store.get_client(client_id)
            books.require_active(client)
        name, sha256 = "Pasted text", ""
        if file is not None and file.filename:
            data = intake.read_limited(file.file, settings.max_upload_bytes)
            item = intake.load_upload(file.filename, data, settings.max_pdf_pages)
            name, sha256 = file.filename, hashlib.sha256(data).hexdigest()
        elif text is not None:
            item = intake.from_text(text, settings.max_upload_bytes)
        else:
            raise UnreadableFile("Provide text or a file to analyse.")
        model_client.ensure_available()   # fail fast with 503 instead of queueing doomed work
        job = jobs.submit(lambda ctx: run_analysis(item, ctx, client, name, sha256))
        return {"job_id": job.id, "status": job.status}
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_clients_api.py tests/test_api.py`
Expected: PASS. `test_the_business_name_setting_reaches_the_model` still passes, because there is no client and the name comes from settings.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_clients_api.py
git commit -m "Analyse for a client: told its name and kind of business, booked with its settings, saved when done

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Excel export

**Files:**
- Modify: `ledgersync/errors.py` (`InvalidTransactions.problems`)
- Modify: `ledgersync/posting.py` (pass the problems)
- Create: `ledgersync/export.py`
- Modify: `ledgersync/ledger.py` (`export`)
- Modify: `server.py` (`GET /api/clients/{id}/export.xlsx`)
- Test: `tests/test_export.py`, `tests/test_clients_api.py`

**Interfaces:**
- Consumes: from Task 7, `ledger.build` and `ledger.trial_balance`.
- Produces:
  - `InvalidTransactions(message, problems=())`, which has `.problems: list[str]`;
  - `export.MONEY = '"£"#,##0.00'` and `export.DATE = "dd/mm/yyyy"`;
  - `export.file_name(client_name, day) -> str`;
  - `export.workbook(ledger, balance: TrialBalance | list[str], day) -> bytes`;
  - `ledger.export(store, client_id, day) -> tuple[bytes, str]`;
  - the endpoint `GET /api/clients/{id}/export.xlsx`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export.py`:

```python
import datetime as dt
import io

import pytest
from openpyxl import load_workbook

from ledgersync import ledger
from ledgersync.export import MONEY, file_name
from ledgersync.models import ClientFields, RowPatch, Transaction
from ledgersync.store import Store

DAY = dt.date(2026, 10, 6)
CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ledgersync.db")


def receipt(**changes):
    fields = dict(direction="out", gross="72.00", vat="12.00", account_code="7502", date=dt.date(2026, 9, 1),
                  description="BT Business, broadband", document_type="receipt", counterparty="BT Business")
    return Transaction(**{**fields, **changes})


def sheets(store, client_id):
    content, name = ledger.export(store, client_id, DAY)
    book = load_workbook(io.BytesIO(content))
    return book["Trial balance"], book["Transactions"], name


def test_the_workbook_has_the_trial_balance_and_the_transactions(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "BT-0905.pdf", "pdf", [receipt()])
    tb, txs, name = sheets(store, client.id)
    assert name == "Business Cube Ltd 2026-10-06.xlsx"
    assert tb["A1"].value == "Business Cube Ltd — trial balance — 6 Oct 2026"
    assert [[c.value for c in row] for row in tb.iter_rows(min_row=2)] == [
        ["Code", "Account", "Debit", "Credit"], ["1200", "Bank Current Account", 0, 72],
        ["2201", "Purchase VAT", 12, 0], ["7502", "Telephone and Internet", 60, 0], [None, "Totals", 72, 72]]
    assert (tb["C3"].number_format, tb.freeze_panes) == (MONEY, "A3")
    assert [c.value for c in txs[2]] == ["Date", "Description", "Counterparty", "Document type", "In/Out", "Amount",
                                         "VAT", "Net", "Account", "Account name", "Other side", "Still owed",
                                         "Issues", "Upload", "Edited"]
    assert [c.value for c in txs[3]] == [dt.datetime(2026, 9, 1), "BT Business, broadband", "BT Business", "Receipt",
                                         "Out", 72, 12, 60, "7502", "Telephone and Internet", "1200", None, None,
                                         "BT-0905.pdf", None]
    assert (txs["A3"].number_format, txs["F3"].number_format, txs.freeze_panes) == ("dd/mm/yyyy", MONEY, "A3")


def test_while_rows_need_fixing_the_trial_balance_sheet_says_what(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [receipt(account_code="9999")])
    tb, _, _ = sheets(store, client.id)
    assert (tb["A2"].value, tb["A3"].value) == ("The trial balance can't be produced yet:",
                                                "#1: Account 9999 is not in the chart of accounts.")


def test_an_edited_row_says_so(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [receipt()])
    ledger.change_row(store, client.id, store.rows(client.id)[0].id, RowPatch(account_code="7504"))
    _, txs, _ = sheets(store, client.id)
    assert (txs["I3"].value, txs["O3"].value) == ("7504", "Yes")


def test_the_file_name_drops_characters_file_names_cannot_hold():
    # Review focus: a client name with characters a file name can't hold, or letters outside ASCII.
    assert file_name('A/B: "Trading" Ltd', DAY) == "A B Trading Ltd 2026-10-06.xlsx"
    assert file_name("Café £ Ltd", DAY) == "Café £ Ltd 2026-10-06.xlsx"
    assert file_name("???", DAY) == "Client 2026-10-06.xlsx"
```

Append to `tests/test_clients_api.py`:

```python
def test_the_export_downloads_with_the_clients_name_even_when_archived(api):
    client = api()
    cid = add(client, name="Café/Bar Ltd")["id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    resp = client.get(f"/api/clients/{cid}/export.xlsx")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''Caf%C3%A9%20Bar%20Ltd%20" in disposition and 'filename="Caf_ Bar Ltd ' in disposition
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_export.py tests/test_clients_api.py`
Expected: FAIL. `test_export.py` fails at collection with `ModuleNotFoundError: No module named 'ledgersync.export'`, and the API test gets 404.

- [ ] **Step 3: Problems as a list**

In `ledgersync/errors.py`, replace the `InvalidTransactions` class with:

```python
class InvalidTransactions(LedgerSyncError):
    status_code = 422
    code = "transactions_need_fixing"

    def __init__(self, message: str, problems=()):
        super().__init__(message)
        self.problems = list(problems)   # one line per problem, for a list such as the Excel export's
```

In `ledgersync/posting.py`, change the `raise` in `trial_balance` to:

```python
        raise InvalidTransactions(f"{len(problems)} problem(s) must be fixed before the trial balance: "
                                  + " ".join(problems[:5]), problems=problems)
```

- [ ] **Step 4: The workbook**

Create `ledgersync/export.py`:

```python
"""A client's work as an Excel workbook (design of 2026-10-06): its trial balance and its transactions, from
the same ledger the pages show. Amounts are numbers in pounds and dates are dates, so the sheets add up."""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Union

from openpyxl import Workbook
from openpyxl.styles import Font

from .models import Direction, Ledger, TrialBalance

MONEY = '"£"#,##0.00'
DATE = "dd/mm/yyyy"
COLUMNS = ("Date", "Description", "Counterparty", "Document type", "In/Out", "Amount", "VAT", "Net", "Account",
           "Account name", "Other side", "Still owed", "Issues", "Upload", "Edited")
_TYPE_NAMES = {"receipt": "Receipt", "invoice": "Invoice", "expense_claim": "Expense claim",
               "statement": "Bank statement", "quote": "Quote", "pro_forma": "Pro forma",
               "purchase_order": "Purchase order", "remittance_advice": "Remittance advice",
               "supplier_statement": "Supplier statement", "other": "Other"}
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def file_name(client_name: str, day: dt.date) -> str:
    """'Business Cube Ltd 2026-10-06.xlsx', without characters that file names can't hold."""
    name = _SPACES.sub(" ", _UNSAFE.sub(" ", client_name)).strip(" .") or "Client"
    return f"{name} {day.isoformat()}.xlsx"


def workbook(ledger: Ledger, balance: Union[TrialBalance, list[str]], day: dt.date) -> bytes:
    """The workbook: a "Trial balance" sheet (or, while rows need fixing, what stops it) and a "Transactions"
    sheet with one row per saved row, in the table's order."""
    book = Workbook()
    _trial_balance(book.active, ledger, balance, day)
    _transactions(book.create_sheet("Transactions"), ledger)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _title(sheet, text: str) -> None:
    sheet.append([text])
    sheet["A1"].font = Font(bold=True)


def _widths(sheet, widths) -> None:
    for n, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + n)].width = width


def _trial_balance(sheet, ledger: Ledger, balance, day: dt.date) -> None:
    sheet.title = "Trial balance"
    _title(sheet, f"{ledger.client.name} — trial balance — {day.day} {day:%b %Y}")
    if isinstance(balance, list):
        sheet.append(["The trial balance can't be produced yet:"])
        for problem in balance:
            sheet.append([problem])
        return
    sheet.append(["Code", "Account", "Debit", "Credit"])
    for line in balance.lines:
        sheet.append([line.code, line.name, line.debit, line.credit])
    sheet.append([None, "Totals", balance.total_debits, balance.total_credits])
    for row in sheet.iter_rows(min_row=3, min_col=3, max_col=4):
        for cell in row:
            cell.number_format = MONEY
    sheet.freeze_panes = "A3"
    _widths(sheet, (8, 34, 14, 14))


def _transactions(sheet, ledger: Ledger) -> None:
    uploads = {u.id: u.name for u in ledger.uploads}
    _title(sheet, f"{ledger.client.name} — transactions")
    sheet.append(list(COLUMNS))
    for tx in ledger.transactions:
        code, name = (tx.paid_against, tx.paid_against_name) if tx.paid_against else (tx.account_code, tx.account_name)
        sheet.append([tx.date, tx.description, tx.counterparty, _TYPE_NAMES.get(tx.document_type, tx.document_type),
                      "In" if tx.direction == Direction.IN else "Out", tx.gross, tx.vat_posted, tx.net, code, name,
                      tx.contra_account_code, tx.owed, "; ".join(i.message for i in tx.issues) or None,
                      uploads.get(tx.upload_id), "Yes" if tx.edited else None])
    for row in sheet.iter_rows(min_row=3):
        row[0].number_format = DATE
        for cell in (*row[5:8], row[11]):
            cell.number_format = MONEY
    sheet.freeze_panes = "A3"
    _widths(sheet, (12, 36, 22, 16, 8, 12, 10, 12, 9, 26, 10, 12, 50, 24, 8))
```

In `ledgersync/ledger.py`:

1. Add `import datetime as dt` to the imports.
2. Change the errors import to `from .errors import ClientArchived, InvalidInput, InvalidTransactions, NotFound`.
3. Add `from .export import file_name, workbook`.
4. Append:

```python
def export(store: Store, client_id: int, day: dt.date) -> tuple[bytes, str]:
    """The client's workbook and its file name; while rows need fixing, the trial balance sheet says what."""
    built = build(store, client_id)
    try:
        balance = trial_balance(store, client_id)
    except InvalidTransactions as exc:
        balance = exc.problems
    return workbook(built, balance, day), file_name(built.client.name, day)
```

In `server.py`:

1. Add `import datetime as dt` and `from urllib.parse import quote` to the imports.
2. Change the responses import to `from fastapi.responses import JSONResponse, Response`.
3. After `client_trial_balance`, add:

```python
    @app.get("/api/clients/{client_id}/export.xlsx")
    def export_client(client_id: int):
        """The client's trial balance and transactions as an Excel workbook (archived clients too)."""
        content, name = books.export(store, client_id, dt.date.today())
        plain = name.encode("ascii", "replace").decode().replace("?", "_")
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=\"{plain}\"; "
                                                        f"filename*=UTF-8''{quote(name)}"})
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_export.py tests/test_clients_api.py`
Expected: PASS.

Run: `.venv/bin/python -m pytest -q`
Expected: no failures. The existing trial-balance 422 tests still pass, because the message is unchanged.

- [ ] **Step 6: Commit**

```bash
git add ledgersync/errors.py ledgersync/posting.py ledgersync/export.py ledgersync/ledger.py server.py tests/test_export.py tests/test_clients_api.py
git commit -m "Export: a client's trial balance and transactions as an Excel workbook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Frontend — client API calls and page rules

**Files:**
- Modify: `frontend/src/lib/api.ts` (export `request`; `AnalyzeResult.client_id` and `upload_id`)
- Create: `frontend/src/lib/clients.ts`, `frontend/src/lib/clientRules.ts`
- Create: `frontend/src/lib/clientRules.test.ts`
- Modify: `frontend/package.json` (the `test` script runs every `src/lib/*.test.ts`)

**Interfaces:**
- Consumes:
  - the Task 8 endpoints;
  - from `lib/ledger.ts`, `money` and `totalsOf`.
- Produces, in `clients.ts`:
  - the types `BusinessType`, `ClientFields`, `Client`, `ClientSummary`, `StatementCheck`, `Upload`, `EditableField`, `SavedRow`, `Ledger`, `RowChange` and `AccountChoice`;
  - the calls `listClients`, `addClient`, `changeClient`, `getLedger`, `changeRow`, `addManualRows`, `removeUpload`, `clientTrialBalance` and `listAccounts`;
  - `exportUrl`.
- Produces, in `clientRules.ts`:
  - `BUSINESS_TYPES` and `DOCUMENT_TYPES`;
  - `dayMonth`, `searchClients`, `clientFormErrors` and `figuresOf`;
  - `sameFileAs` and `statementText`;
  - `EditDraft`, `draftOf`, `rowEditErrors` and `changesOf`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/clientRules.test.ts`:

```ts
// Run with: npm test (Node's own test runner; Node strips the TypeScript types itself).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ClientSummary, SavedRow, Upload } from './clients.ts'
import {
  changesOf, clientFormErrors, dayMonth, draftOf, figuresOf, rowEditErrors, sameFileAs, searchClients, statementText,
} from './clientRules.ts'

const client = (change: Partial<ClientSummary> = {}): ClientSummary => ({
  id: 1, name: 'Business Cube Ltd', business_type: 'limited_company', contact_name: 'Jenny Clarke',
  contact_email: 'jenny@businesscube.co.uk', contact_phone: '', vat_registered: true, archived: false,
  archived_at: null, created_at: '2026-10-06T09:00:00+00:00', updated_at: '2026-10-06T09:00:00+00:00',
  rows: 0, to_review: 0, ...change,
})

const row = (change: Partial<SavedRow> = {}): SavedRow => ({
  id: 1, upload_id: 1, edited: false, original: null,
  date: '2026-10-01', description: 'October rent', direction: 'out', gross: '12000.00', vat: '2000.00',
  vat_treatment: null, vat_posted: '2000.00', net: '10000.00', account_code: '7100', contra_account_code: '2100',
  currency: 'GBP', source: 'pdf', method: 'llm', evidence: null, issues: [], document_type: 'invoice',
  counterparty: 'Business Cube', document_ref: 'rent', owed: '12000.00', paid_by: [], ...change,
})

test('search finds clients by company, responsible person or email, ignoring case', () => {
  const clients = [client(), client({ id: 2, name: 'Harbour & Lane LLP', contact_name: 'Matt Fisher',
                                      contact_email: 'matt@harbour.co.uk' })]
  assert.deepEqual(searchClients(clients, 'harbour').map(c => c.id), [2])
  assert.deepEqual(searchClients(clients, 'JENNY').map(c => c.id), [1])
  assert.deepEqual(searchClients(clients, '  ').map(c => c.id), [1, 2])
})

test('the client dialog needs a company name, a responsible person and a real email', () => {
  const fields = { name: ' ', business_type: 'limited_company' as const, contact_name: '', contact_email: 'jenny',
                   contact_phone: '', vat_registered: true }
  assert.deepEqual(clientFormErrors(fields), {
    name: 'Enter the company name', contact_name: 'Enter the responsible person', contact_email: 'Enter a valid email',
  })
  assert.deepEqual(clientFormErrors({ ...fields, name: 'Cube', contact_name: 'Jenny', contact_email: '' }), {})
})

test('the figures count the rows to review and what is still owed each way', () => {
  const sale = row({ id: 2, direction: 'in', account_code: '4000', document_ref: 'sale', gross: '500.00',
                     owed: '500.00', issues: [{ code: 'x', severity: 'warning', message: 'm' }] })
  assert.deepEqual(figuresOf([row(), sale]), { rows: 2, toReview: 1, toPay: '12000.00', toReceive: '500.00' })
})

test('a picked file that repeats a saved upload is found by its fingerprint', () => {
  const uploads = [{ id: 7, name: 'BT-0905.pdf', sha256: 'abc' } as Upload]
  assert.equal(sameFileAs('abc', uploads)?.name, 'BT-0905.pdf')
  assert.equal(sameFileAs(null, uploads), undefined)
  assert.equal(sameFileAs('zzz', uploads), undefined)
})

test('a statement upload says whether its balances add up', () => {
  assert.equal(statementText({ status: 'ok', difference: null }), 'Balances add up')
  assert.equal(statementText({ status: 'gap', difference: '12.40' }), "Doesn't add up: £12.40")
  assert.equal(statementText({ status: 'none', difference: null }), 'No balances to check')
  assert.equal(statementText(null), null)
})

test('the edit form checks the amount, the VAT and the description', () => {
  const draft = draftOf(row())
  assert.deepEqual(rowEditErrors(draft), {})
  assert.deepEqual(rowEditErrors({ ...draft, gross: '0' }), { gross: 'Enter an amount above zero' })
  assert.deepEqual(rowEditErrors({ ...draft, vat: '12000' }), { vat: 'VAT must be below the amount' })
  assert.deepEqual(rowEditErrors({ ...draft, vat: '-1', description: ' ' }),
                   { vat: "VAT can't be negative", description: 'Enter a description' })
})

test('an edit sends only what changed, with empty text as none', () => {
  const draft = { ...draftOf(row()), gross: '12000', account_code: '7103', vat: '', counterparty: ' Business Cube ' }
  assert.deepEqual(changesOf(row(), draft), { account_code: '7103', vat: null })
})

test('dates read as day and short month', () => {
  assert.equal(dayMonth('2026-10-06'), '6 Oct')
  assert.equal(dayMonth('2026-10-06T12:00:00+00:00'), '6 Oct')
  assert.equal(dayMonth(null), '')
})
```

In `frontend/package.json`, change the `test` script to:

```json
    "test": "node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON --test 'src/lib/*.test.ts'"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && npm test`
Expected: FAIL with `Cannot find module …/src/lib/clientRules.ts`. The 4 `ledger.test.ts` tests still pass.

- [ ] **Step 3: The API calls**

In `frontend/src/lib/api.ts`:

1. Change `async function request<T>(` to `export async function request<T>(`.
2. Add these fields to `AnalyzeResult`:

```ts
  client_id?: number    // set when the analysis was for a client: its rows were saved
  upload_id?: number
```

Create `frontend/src/lib/clients.ts`:

```ts
// Calls for clients and their saved ledgers (design of 2026-10-06). Paths go through the Next rewrite,
// like every other call in api.ts.
import { request, type Transaction, type TrialBalanceResult } from './api'

export type BusinessType = 'limited_company' | 'sole_trader' | 'partnership' | 'llp'

export interface ClientFields {
  name: string
  business_type: BusinessType
  contact_name: string       // the responsible person: the client's own contact
  contact_email: string
  contact_phone: string
  vat_registered: boolean
}

export interface Client extends ClientFields {
  id: number
  archived: boolean
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface ClientSummary extends Client {
  rows: number
  to_review: number          // rows with an error or a warning
}

export interface StatementCheck {
  status: 'ok' | 'gap' | 'none'
  difference: string | null
}

export interface Upload {
  id: number
  name: string               // the file's name, "Pasted text" or "Manual entry"
  kind: string
  sha256: string             // the file's fingerprint, to skip a file uploaded before
  model: string
  warnings: string[]
  created_at: string
  rows: number
  statement: StatementCheck | null
}

export type EditableField =
  'date' | 'description' | 'counterparty' | 'direction' | 'gross' | 'vat' | 'account_code' | 'document_type'

export interface SavedRow extends Transaction {
  id: number
  upload_id: number
  edited: boolean
  original: Partial<Record<EditableField, string | null>> | null   // what was read, once a person edits
}

export interface Ledger {
  client: Client
  uploads: Upload[]
  transactions: SavedRow[]
}

// A person's change to a row: Link or Include, an edit, or revert.
export type RowChange = Partial<Record<EditableField, string | null>> & {
  link?: string[] | null
  include?: boolean
  revert?: boolean
}

export interface AccountChoice {
  code: string
  name: string
  type: string
  vat: string
}

const send = (method: string, body: unknown): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export const listClients = (archived = false) => request<ClientSummary[]>(`/api/clients?archived=${archived}`)
export const addClient = (fields: ClientFields) => request<Client>('/api/clients', send('POST', fields))
export const changeClient = (id: number, change: Partial<ClientFields> & { archived?: boolean }) =>
  request<Client>(`/api/clients/${id}`, send('PATCH', change))
export const getLedger = (id: number) => request<Ledger>(`/api/clients/${id}/ledger`)
export const changeRow = (id: number, rowId: number, change: RowChange) =>
  request<Ledger>(`/api/clients/${id}/rows/${rowId}`, send('PATCH', change))
export const addManualRows = (id: number, transactions: Transaction[]) =>
  request<Ledger>(`/api/clients/${id}/uploads`, send('POST', { name: 'Manual entry', kind: 'manual', transactions }))
export const removeUpload = (id: number, uploadId: number) =>
  request<Ledger>(`/api/clients/${id}/uploads/${uploadId}`, { method: 'DELETE' })
export const clientTrialBalance = (id: number) => request<TrialBalanceResult>(`/api/clients/${id}/trial-balance`)
export const listAccounts = (businessType: BusinessType) =>
  request<AccountChoice[]>(`/api/accounts?business_type=${businessType}`)
export const exportUrl = (id: number) => `/api/clients/${id}/export.xlsx`
```

- [ ] **Step 4: The page rules**

Create `frontend/src/lib/clientRules.ts`:

```ts
// Rules the client pages share, kept apart from the network so node --test can run them.
import type { Transaction } from './api'
import type { BusinessType, ClientFields, ClientSummary, EditableField, RowChange, SavedRow, StatementCheck, Upload } from './clients'
import { money, totalsOf } from './ledger.ts'

export const BUSINESS_TYPES: Record<BusinessType, string> = {
  limited_company: 'Limited company', sole_trader: 'Sole trader', partnership: 'Partnership', llp: 'LLP',
}

export const DOCUMENT_TYPES: Record<string, string> = {
  receipt: 'Receipt', invoice: 'Invoice', expense_claim: 'Expense claim', statement: 'Bank statement line',
  quote: 'Quote', pro_forma: 'Pro forma invoice', purchase_order: 'Purchase order',
  remittance_advice: 'Remittance advice', supplier_statement: "Supplier's statement", other: 'Other document',
}

// "6 Oct", from an ISO date or time.
export function dayMonth(iso: string | null | undefined): string {
  if (!iso) return ''
  const when = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso)
  return Number.isNaN(when.getTime()) ? '' : when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

// The clients whose company, responsible person or email holds the search text, ignoring case.
export function searchClients(clients: ClientSummary[], query: string): ClientSummary[] {
  const wanted = query.trim().toLowerCase()
  if (!wanted) return clients
  return clients.filter(c => [c.name, c.contact_name, c.contact_email].some(text => text.toLowerCase().includes(wanted)))
}

export type ClientErrors = Partial<Record<'name' | 'contact_name' | 'contact_email', string>>

// What the client dialog says before saving; the API checks the same.
export function clientFormErrors(fields: ClientFields): ClientErrors {
  const errors: ClientErrors = {}
  if (!fields.name.trim()) errors.name = 'Enter the company name'
  if (!fields.contact_name.trim()) errors.contact_name = 'Enter the responsible person'
  if (fields.contact_email.trim() && !fields.contact_email.includes('@')) errors.contact_email = 'Enter a valid email'
  return errors
}

// The four figures at the top of a client's page.
export function figuresOf(rows: SavedRow[]) {
  const totals = totalsOf(rows)
  const owed = (key: string) => totals.find(t => t.key === key)?.stillOwed ?? '0.00'
  return {
    rows: rows.length,
    toReview: rows.filter(row => row.issues.some(i => i.severity === 'error' || i.severity === 'warning')).length,
    toPay: owed('documents-out'),
    toReceive: owed('documents-in'),
  }
}

// The saved upload a picked file repeats, by its fingerprint.
export const sameFileAs = (hash: string | null, uploads: Upload[]) =>
  (hash ? uploads.find(upload => upload.sha256 === hash) : undefined)

// What the uploads list says about a bank statement's balances; null for any other upload.
export function statementText(check: StatementCheck | null): string | null {
  if (!check) return null
  if (check.status === 'ok') return 'Balances add up'
  if (check.status === 'gap') return `Doesn't add up: ${money(check.difference)}`
  return 'No balances to check'
}

// The edit form's fields, as text.
export type EditDraft = Record<EditableField, string>
export type EditErrors = Partial<Record<EditableField, string>>

export function draftOf(row: Transaction): EditDraft {
  return {
    date: row.date ?? '', description: row.description, counterparty: row.counterparty ?? '',
    direction: row.direction, gross: row.gross, vat: row.vat ?? '', account_code: row.account_code,
    document_type: row.document_type ?? 'receipt',
  }
}

// What the edit form says before saving; the API checks the same.
export function rowEditErrors(draft: EditDraft): EditErrors {
  const errors: EditErrors = {}
  const gross = Number(draft.gross)
  const vat = draft.vat.trim() === '' ? null : Number(draft.vat)
  if (!draft.description.trim()) errors.description = 'Enter a description'
  if (draft.date && !/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) errors.date = 'Enter a date'
  if (!draft.gross.trim() || !Number.isFinite(gross) || gross <= 0) errors.gross = 'Enter an amount above zero'
  if (vat !== null && (!Number.isFinite(vat) || vat < 0)) errors.vat = "VAT can't be negative"
  else if (vat !== null && !errors.gross && vat >= gross) errors.vat = 'VAT must be below the amount'
  return errors
}

// Only what the person changed, with empty text sent as none (no date, no VAT shown, no counterparty).
export function changesOf(row: Transaction, draft: EditDraft): RowChange {
  const before = draftOf(row)
  const change: RowChange = {}
  for (const field of Object.keys(draft) as EditableField[]) {
    const value = draft[field].trim()
    const was = before[field].trim()
    const sameAmount = (field === 'gross' || field === 'vat') && value !== '' && was !== '' && Number(value) === Number(was)
    if (value === was || sameAmount) continue
    change[field] = value === '' && (field === 'date' || field === 'vat' || field === 'counterparty') ? null : value
  }
  return change
}
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && npm test`
Expected: PASS. That is 12 tests: 4 in `ledger.test.ts` and 8 in `clientRules.test.ts`.

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/clients.ts frontend/src/lib/clientRules.ts frontend/src/lib/clientRules.test.ts frontend/package.json
git commit -m "Frontend: calls for clients and saved ledgers, and the rules the client pages share

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Frontend — the Ledger look, the header and the clients page

Before writing code, read these pages in `frontend/node_modules/next/dist/docs/01-app/03-api-reference/`:
- `02-components/font.md`;
- `02-components/link.md`;
- `04-functions/use-router.md`.

**Files:**
- Rewrite: `frontend/src/app/globals.css`, `frontend/src/app/layout.tsx`
- Rewrite: `frontend/src/app/page.tsx`. The clients page replaces the old single page; its analysis and table parts are rebuilt in Tasks 13–15.
- Create:
  - `frontend/src/lib/theme.ts` and `frontend/src/lib/theme.test.ts`;
  - `frontend/src/components/AppHeader.tsx`, `ConfirmDialog.tsx` and `ClientDialog.tsx`.
- Modify: `.claude/launch.json`, which is git-ignored, by adding a spare-port API with a scratch database.

**Interfaces:**
- Consumes: Task 11's `clients.ts` and `clientRules.ts`.
- Produces:
  - from `theme.ts`: `THEME_KEY`, `themeScript` and `startTheme(saved, prefersDark) -> 'light' | 'dark'`;
  - `useConfirm() -> { ask(text, action): Promise<boolean>, dialog }`;
  - `<ClientDialog client? onCancel onSave(fields) />`;
  - the CSS classes every later component uses.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/theme.test.ts`:

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { startTheme, themeScript, THEME_KEY } from './theme.ts'

test('a saved choice wins, otherwise the computer setting', () => {
  assert.equal(startTheme('dark', false), 'dark')
  assert.equal(startTheme('light', true), 'light')
  assert.equal(startTheme(null, true), 'dark')
  assert.equal(startTheme('purple', false), 'light')
})

test('the early script reads the key the switch writes', () => {
  assert.ok(themeScript.includes(`localStorage.getItem('${THEME_KEY}')`))
})
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd frontend && npm test`
Expected: FAIL with `Cannot find module …/src/lib/theme.ts`.

- [ ] **Step 3: The theme**

Create `frontend/src/lib/theme.ts`:

```ts
// The light/dark choice (design of 2026-10-06): kept in this browser, otherwise the computer's setting.
export type Theme = 'light' | 'dark'
export const THEME_KEY = 'ledgersync-theme'

// Runs in the page's head before it paints, so a saved choice never flashes the other theme first.
export const themeScript =
  `(function(){try{var t=localStorage.getItem('${THEME_KEY}');` +
  `if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}})()`

export function startTheme(saved: string | null, prefersDark: boolean): Theme {
  return saved === 'light' || saved === 'dark' ? saved : prefersDark ? 'dark' : 'light'
}
```

Run: `cd frontend && npm test`
Expected: PASS (14 tests).

- [ ] **Step 4: The look**

Replace all of `frontend/src/app/globals.css` with:

```css
/* LedgerSync: the "Ledger" look (design of 2026-10-06). Warm paper, ink and one green accent; serif headings
   and figures in a fixed-width face, so pounds and pence line up. Light on :root; dark under
   [data-theme="dark"], or by the computer's setting unless a person chose light. */

:root {
  --page: #FAF8F3;
  --card: #FFFFFF;
  --text: #1F2421;
  --muted: #5F5E5A;
  --rule: #E4E0D6;
  --accent: #0F6E56;
  --accent-tint: #E1F5EE;
  --on-tint: #085041;
  --on-accent: #FFFFFF;
  --warn: #854F0B;
  --warn-tint: #FAEEDA;
  --error: #A32D2D;
  --error-tint: #FCEBEB;
  --ok: #3B6D11;
  --hover: rgba(31, 36, 33, 0.05);
  --overlay: rgba(31, 36, 33, 0.4);
  --radius: 8px;
  color-scheme: light;
}

:root[data-theme="dark"] {
  --page: #151714;
  --card: #1D201C;
  --text: #ECEAE3;
  --muted: #A3A199;
  --rule: #30342E;
  --accent: #1D9E75;
  --accent-tint: #085041;
  --on-tint: #9FE1CB;
  --on-accent: #04342C;
  --warn: #FAC775;
  --warn-tint: #412402;
  --error: #F09595;
  --error-tint: #501313;
  --ok: #97C459;
  --hover: rgba(236, 234, 227, 0.06);
  --overlay: rgba(0, 0, 0, 0.55);
  color-scheme: dark;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page: #151714;
    --card: #1D201C;
    --text: #ECEAE3;
    --muted: #A3A199;
    --rule: #30342E;
    --accent: #1D9E75;
    --accent-tint: #085041;
    --on-tint: #9FE1CB;
    --on-accent: #04342C;
    --warn: #FAC775;
    --warn-tint: #412402;
    --error: #F09595;
    --error-tint: #501313;
    --ok: #97C459;
    --hover: rgba(236, 234, 227, 0.06);
    --overlay: rgba(0, 0, 0, 0.55);
    color-scheme: dark;
  }
}

*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--page);
  color: var(--text);
  font-family: var(--font-sans), system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3 { font-family: var(--font-serif), Georgia, serif; font-weight: 500; margin: 0; }
h1 { font-size: 26px; line-height: 1.2; }
h2 { font-size: 18px; }
p { margin: 0; }
a { color: var(--accent); }
button, input, select, textarea { font: inherit; color: inherit; }
.page { max-width: 1360px; margin: 0 auto; padding: 24px 24px 64px; }

/* Header */
.app-header {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 12px 24px; border-bottom: 1px solid var(--rule); background: var(--card);
}
.brand { display: inline-flex; align-items: center; gap: 10px; font-weight: 600; font-size: 15px; color: var(--text); text-decoration: none; }
.brand-mark {
  width: 26px; height: 26px; border-radius: 7px; display: grid; place-items: center;
  background: var(--accent); color: var(--on-accent); font-family: var(--font-serif), serif;
}
.header-end { display: flex; align-items: center; gap: 14px; }
.ai-status { font-size: 12.5px; color: var(--muted); }
.ai-status::before { content: '●'; margin-right: 6px; }
.ai-on::before { color: var(--ok); }
.ai-off::before { color: var(--error); }
.icon-btn { width: 32px; height: 32px; border-radius: var(--radius); border: 1px solid var(--rule); background: transparent; cursor: pointer; }
.icon-btn:hover { background: var(--hover); }

/* Page heads */
.page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin: 8px 0 20px; }
.page-head p { margin-top: 4px; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.crumbs { font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.crumbs a { color: var(--muted); }
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }

/* Buttons and fields */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: 36px; padding: 0 14px;
  border-radius: var(--radius); border: 1px solid var(--rule); background: var(--card); color: var(--text);
  font-weight: 500; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.btn:hover { background: var(--hover); }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.btn-primary:hover { background: var(--accent); filter: brightness(1.08); }
.btn-ghost { background: transparent; }
.btn-danger { background: var(--error-tint); border-color: var(--error-tint); color: var(--error); }
.btn-danger:hover { background: var(--error-tint); filter: brightness(0.97); }
.link-btn {
  background: none; border: 0; padding: 0; margin-left: 10px; color: var(--accent); cursor: pointer;
  text-decoration: underline; text-underline-offset: 2px; font-size: inherit;
}
.link-btn.danger { color: var(--error); }
.input { width: 100%; height: 36px; padding: 0 10px; border: 1px solid var(--rule); border-radius: var(--radius); background: var(--card); }
textarea.input { height: auto; min-height: 150px; padding: 10px; resize: vertical; }
.search { max-width: 360px; }
.btn:focus-visible, .link-btn:focus-visible, .icon-btn:focus-visible, .input:focus-visible, .tab:focus-visible, .dropzone:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
.field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; font-size: 13px; color: var(--muted); }
.field > span:first-child { color: var(--text); font-weight: 500; }
.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field-error { color: var(--error); font-size: 12.5px; }
.switch { display: flex; align-items: center; gap: 8px; margin: 4px 0 14px; cursor: pointer; }

/* Cards, sections, notices, figures */
.card { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; }
.section { padding: 16px 18px; margin-bottom: 16px; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.table-card { overflow-x: auto; }
.notice { padding: 10px 14px; border-radius: var(--radius); margin-bottom: 14px; font-size: 13.5px; }
.section > .notice { margin: 12px 0 0; }
.notice-error { background: var(--error-tint); color: var(--error); }
.notice-warn { background: var(--warn-tint); color: var(--warn); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.figures { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.figure { padding: 12px 14px; }
.figure-label { font-size: 12px; color: var(--muted); }
.figure-value { font-family: var(--font-mono), ui-monospace, monospace; font-variant-numeric: tabular-nums; font-size: 20px; margin-top: 2px; }
.empty { text-align: center; padding: 56px 16px; }
.empty p { margin: 8px 0 18px; }

/* Tables */
.table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.table th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; padding: 10px 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
.table td { padding: 10px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
.table tbody tr:last-child td { border-bottom: 0; }
.table tfoot td { border-top: 1px solid var(--rule); border-bottom: 0; font-weight: 500; }
.num, .table .num { text-align: right; font-family: var(--font-mono), ui-monospace, monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mono { font-family: var(--font-mono), ui-monospace, monospace; font-variant-numeric: tabular-nums; }
.clickable { cursor: pointer; }
.clickable:hover { background: var(--hover); }
.row-actions { white-space: nowrap; text-align: right; }
.row-not-booked td { opacity: 0.6; }
.row-origin { font-size: 12px; color: var(--muted); max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-status { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
.row-status input { margin-right: 6px; vertical-align: -2px; }
.check { white-space: nowrap; font-size: 12.5px; }
.check-ok { color: var(--ok); }
.check-info { color: var(--muted); }
.check-warn { color: var(--warn); }
.check-error { color: var(--error); }

/* Badges and text */
.badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font: 500 11.5px var(--font-sans), sans-serif; vertical-align: middle; }
.badge-accent { background: var(--accent-tint); color: var(--on-tint); }
.badge-warn { background: var(--warn-tint); color: var(--warn); }
.badge-muted { background: var(--hover); color: var(--muted); }
.edited { margin-left: 8px; }
.tb-pill { margin-top: 12px; }
.muted { color: var(--muted); }
.small { font-size: 12.5px; }
.strong { font-weight: 600; color: var(--text); text-decoration: none; }
.strong:hover { text-decoration: underline; }
.text-warn { color: var(--warn); }
.text-ok { color: var(--ok); }

/* Dialogs */
.overlay { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 16px; background: var(--overlay); }
.dialog {
  width: min(540px, 100%); max-height: calc(100vh - 32px); overflow-y: auto;
  background: var(--card); border: 1px solid var(--rule); border-radius: 12px; padding: 20px 22px;
}
.dialog-small { width: min(420px, 100%); }
.dialog h2 { margin-bottom: 16px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.dialog-note { font-size: 12.5px; color: var(--muted); background: var(--hover); border-radius: var(--radius); padding: 8px 10px; margin-bottom: 14px; }

/* Adding documents */
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--rule); margin-bottom: 14px; }
.tab { background: none; border: 0; border-bottom: 2px solid transparent; padding: 8px 12px; cursor: pointer; color: var(--muted); font-weight: 500; }
.tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
.dropzone { border: 1.5px dashed var(--rule); border-radius: 10px; padding: 28px; text-align: center; cursor: pointer; color: var(--muted); }
.dropzone:hover, .dropzone.drag-over { border-color: var(--accent); background: var(--hover); }
.dropzone strong { display: block; color: var(--text); font-weight: 500; margin-bottom: 4px; }
.form-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.manual-grid { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 12px; }
.progress { display: flex; align-items: center; gap: 10px; margin-top: 12px; font-size: 13px; color: var(--muted); }
.batch { list-style: none; margin: 12px 0 0; padding: 0; font-size: 13px; }
.batch li { display: grid; grid-template-columns: 20px minmax(0, 1fr) minmax(0, 1.4fr); gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--rule); }
.batch-name, .batch-detail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.batch-detail { color: var(--muted); }
.batch-failed .batch-detail { color: var(--error); }
.spinner {
  width: 14px; height: 14px; border: 2px solid var(--rule); border-top-color: var(--accent); border-radius: 50%;
  display: inline-block; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* A client's page */
.workspace { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; margin-bottom: 16px; }
.workspace > .card { margin-bottom: 0; }
.upload-list { list-style: none; margin: 0; padding: 0; }
.upload-list li { padding: 8px 0; border-bottom: 1px solid var(--rule); }
.upload-list li:last-child { border-bottom: 0; }
.upload-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.upload-list .link-btn { margin-left: 0; font-size: 12.5px; }

@media (min-width: 1400px) { .workspace { grid-template-columns: minmax(0, 1fr) 280px; } }
@media (max-width: 1100px) {
  .figures { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .manual-grid { grid-template-columns: 1fr; }
}
```

- [ ] **Step 5: The layout and the header**

Replace all of `frontend/src/app/layout.tsx` with:

```tsx
import type { Metadata } from 'next'
import { IBM_Plex_Mono, Inter, Source_Serif_4 } from 'next/font/google'
import AppHeader from '@/components/AppHeader'
import { themeScript } from '@/lib/theme'
import './globals.css'

const sans = Inter({ subsets: ['latin'], variable: '--font-sans', display: 'swap' })
const serif = Source_Serif_4({ subsets: ['latin'], variable: '--font-serif', display: 'swap' })
const mono = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500'], variable: '--font-mono', display: 'swap' })

export const metadata: Metadata = {
  title: 'LedgerSync',
  description: "Clients' documents read by AI, booked, matched and saved, with their trial balances",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-GB" className={`${sans.variable} ${serif.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        {/* Applies a saved light/dark choice before the page paints. */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body suppressHydrationWarning>
        <AppHeader />
        <main className="page">{children}</main>
      </body>
    </html>
  )
}
```

Create `frontend/src/components/AppHeader.tsx`:

```tsx
'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { getHealth, type Health } from '@/lib/api'
import { startTheme, THEME_KEY, type Theme } from '@/lib/theme'

// The bar on every page: the app's name (back to the clients), whether the AI model can be reached, and the
// light/dark switch. The switch's choice is kept in this browser; the layout applies it before painting.
export default function AppHeader() {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState('')
  const [theme, setTheme] = useState<Theme>('light')

  useEffect(() => {
    let alive = true
    const check = () => getHealth()
      .then(h => { if (alive) { setHealth(h); setHealthError('') } })
      .catch((e: unknown) => { if (alive) { setHealth(null); setHealthError(e instanceof Error ? e.message : String(e)) } })
    check()
    const timer = setInterval(check, 30_000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  useEffect(() => {
    let saved: string | null = null
    try { saved = localStorage.getItem(THEME_KEY) } catch { /* storage blocked: follow the computer */ }
    setTheme(startTheme(saved, window.matchMedia('(prefers-color-scheme: dark)').matches))
  }, [])

  const flip = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    try { localStorage.setItem(THEME_KEY, next) } catch { /* applied, just not remembered */ }
    setTheme(next)
  }

  const online = !!health?.model_available
  const status = health ? (online ? `AI online · ${health.model}` : 'AI offline') : healthError ? 'API offline' : 'Checking…'
  const switchTo = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
  return (
    <header className="app-header">
      <Link href="/" className="brand"><span className="brand-mark" aria-hidden="true">£</span>LedgerSync</Link>
      <div className="header-end">
        <span className={`ai-status ${online ? 'ai-on' : 'ai-off'}`} title={health?.ai_error || healthError}>{status}</span>
        <button type="button" className="icon-btn" onClick={flip} aria-label={switchTo} title={switchTo}>
          {theme === 'dark' ? '☀' : '☾'}
        </button>
      </div>
    </header>
  )
}
```

- [ ] **Step 6: The confirmation and client dialogs**

Create `frontend/src/components/ConfirmDialog.tsx`:

```tsx
'use client'

import { useCallback, useRef, useState } from 'react'

// A question asked before something hard to undo, in the page's own style: ask() resolves true on confirm.
export function useConfirm() {
  const [question, setQuestion] = useState<{ text: string; action: string } | null>(null)
  const answer = useRef<(yes: boolean) => void>(() => {})

  const ask = useCallback((text: string, action: string) => new Promise<boolean>(resolve => {
    answer.current = resolve
    setQuestion({ text, action })
  }), [])

  const reply = (yes: boolean) => {
    setQuestion(null)
    answer.current(yes)
  }

  const dialog = question && (
    <div className="overlay" role="presentation" onKeyDown={e => { if (e.key === 'Escape') reply(false) }}>
      <div className="dialog dialog-small" role="alertdialog" aria-modal="true" aria-describedby="confirm-text">
        <p id="confirm-text">{question.text}</p>
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={() => reply(false)} autoFocus>Cancel</button>
          <button type="button" className="btn btn-danger" onClick={() => reply(true)}>{question.action}</button>
        </div>
      </div>
    </div>
  )
  return { ask, dialog }
}
```

Create `frontend/src/components/ClientDialog.tsx`:

```tsx
'use client'

import { useState, type FormEvent } from 'react'
import type { BusinessType, Client, ClientFields } from '@/lib/clients'
import { BUSINESS_TYPES, clientFormErrors } from '@/lib/clientRules'

const NEW_CLIENT: ClientFields = {
  name: '', business_type: 'limited_company', contact_name: '', contact_email: '', contact_phone: '', vat_registered: true,
}

// Adds a client, or edits one. The responsible person is the client's own contact.
export default function ClientDialog({ client, onCancel, onSave }: {
  client?: Client
  onCancel: () => void
  onSave: (fields: ClientFields) => Promise<void>
}) {
  const [fields, setFields] = useState<ClientFields>(() => (client ? {
    name: client.name, business_type: client.business_type, contact_name: client.contact_name,
    contact_email: client.contact_email, contact_phone: client.contact_phone, vat_registered: client.vat_registered,
  } : NEW_CLIENT))
  const [showErrors, setShowErrors] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const errors = clientFormErrors(fields)
  const set = <K extends keyof ClientFields>(key: K, value: ClientFields[K]) => setFields(f => ({ ...f, [key]: value }))

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setShowErrors(true)
    if (Object.keys(errors).length) return
    setSaving(true)
    setError('')
    try {
      await onSave(fields)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  return (
    <div className="overlay" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) onCancel() }}
         onKeyDown={e => { if (e.key === 'Escape') onCancel() }}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="client-dialog-title" onSubmit={submit} noValidate>
        <h2 id="client-dialog-title">{client ? 'Edit client' : 'Add client'}</h2>
        <label className="field">
          <span>Company name</span>
          <input className="input" autoFocus value={fields.name} onChange={e => set('name', e.target.value)}
                 placeholder="Business Cube Ltd" />
          {showErrors && errors.name && <span className="field-error">{errors.name}</span>}
        </label>
        <label className="field">
          <span>Business type</span>
          <select className="input" value={fields.business_type}
                  onChange={e => set('business_type', e.target.value as BusinessType)}>
            {Object.entries(BUSINESS_TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Responsible person</span>
          <input className="input" value={fields.contact_name} onChange={e => set('contact_name', e.target.value)}
                 placeholder="Jenny Clarke" />
          {showErrors && errors.contact_name && <span className="field-error">{errors.contact_name}</span>}
        </label>
        <div className="field-row">
          <label className="field">
            <span>Email</span>
            <input className="input" type="email" value={fields.contact_email}
                   onChange={e => set('contact_email', e.target.value)} placeholder="jenny@businesscube.co.uk" />
            {showErrors && errors.contact_email && <span className="field-error">{errors.contact_email}</span>}
          </label>
          <label className="field">
            <span>Phone</span>
            <input className="input" type="tel" value={fields.contact_phone}
                   onChange={e => set('contact_phone', e.target.value)} placeholder="07700 900123" />
          </label>
        </div>
        <label className="switch">
          <input type="checkbox" checked={fields.vat_registered} onChange={e => set('vat_registered', e.target.checked)} />
          <span>VAT registered</span>
        </label>
        {error && <div className="notice notice-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? 'Saving…' : client ? 'Save changes' : 'Add client'}
          </button>
        </div>
      </form>
    </div>
  )
}
```

- [ ] **Step 7: The clients page**

Replace all of `frontend/src/app/page.tsx` with:

```tsx
'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useCallback, useEffect, useMemo, useState } from 'react'
import ClientDialog from '@/components/ClientDialog'
import { useConfirm } from '@/components/ConfirmDialog'
import { addClient, changeClient, listClients, type Client, type ClientFields, type ClientSummary } from '@/lib/clients'
import { BUSINESS_TYPES, dayMonth, searchClients } from '@/lib/clientRules'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// The first page: every client, what needs a look, and the way in to each one.
export default function ClientsPage() {
  const router = useRouter()
  const { ask, dialog: confirmDialog } = useConfirm()
  const [showArchived, setShowArchived] = useState(false)
  const [clients, setClients] = useState<ClientSummary[] | null>(null)
  const [counts, setCounts] = useState({ active: 0, archived: 0 })
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<{ client?: Client } | null>(null)   // the dialog: adding, or editing one

  const load = useCallback(async () => {
    try {
      const [active, archived] = await Promise.all([listClients(false), listClients(true)])
      setCounts({ active: active.length, archived: archived.length })
      setClients(showArchived ? archived : active)
      setError('')
    } catch (e) {
      setError(message(e))
    }
  }, [showArchived])

  useEffect(() => { load() }, [load])

  const shown = useMemo(() => searchClients(clients ?? [], query), [clients, query])

  const save = async (fields: ClientFields) => {
    if (editing?.client) await changeClient(editing.client.id, fields)
    else await addClient(fields)
    setEditing(null)
    await load()
  }

  const archive = async (client: ClientSummary) => {
    if (!(await ask(`Archive ${client.name}? You can restore it from Show archived.`, 'Archive'))) return
    try { await changeClient(client.id, { archived: true }); await load() } catch (e) { setError(message(e)) }
  }

  const restore = async (client: ClientSummary) => {
    try { await changeClient(client.id, { archived: false }); await load() } catch (e) { setError(message(e)) }
  }

  const empty = clients !== null && !clients.length && !showArchived
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Clients</h1>
          <p className="muted">{counts.active} active · {counts.archived} archived</p>
        </div>
        {!showArchived && !empty && (
          <button type="button" className="btn btn-primary" onClick={() => setEditing({})}>Add client</button>
        )}
      </div>
      {error && <div className="notice notice-error">{error}</div>}
      {empty ? (
        <div className="empty card">
          <h2>Add your first client</h2>
          <p className="muted">Each client keeps its documents, its transactions and its trial balance.</p>
          <button type="button" className="btn btn-primary" onClick={() => setEditing({})}>Add client</button>
        </div>
      ) : (
        <>
          <div className="toolbar">
            <input className="input search" type="search" value={query} onChange={e => setQuery(e.target.value)}
                   placeholder="Search clients or contacts" aria-label="Search clients or contacts" />
            <button type="button" className="btn btn-ghost" onClick={() => setShowArchived(s => !s)}>
              {showArchived ? 'Show active' : 'Show archived'}
            </button>
          </div>
          <div className="card table-card">
            <table className="table">
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Responsible person</th>
                  <th>VAT</th>
                  <th className="num">Rows</th>
                  <th className="num">To review</th>
                  <th className="num">Updated</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {clients === null && <tr><td colSpan={7} className="muted">Loading clients…</td></tr>}
                {shown.map(c => (
                  <tr key={c.id} className="clickable" onClick={() => router.push(`/clients/${c.id}`)}>
                    <td>
                      <Link href={`/clients/${c.id}`} className="strong" onClick={e => e.stopPropagation()}>{c.name}</Link>
                      <div className="muted small">{BUSINESS_TYPES[c.business_type]}</div>
                    </td>
                    <td>
                      {c.contact_name}
                      <div className="muted small">{[c.contact_email, c.contact_phone].filter(Boolean).join(' · ')}</div>
                    </td>
                    <td>
                      <span className={c.vat_registered ? 'badge badge-accent' : 'badge badge-warn'}>
                        {c.vat_registered ? 'Registered' : 'Not registered'}
                      </span>
                    </td>
                    <td className="num">{c.rows}</td>
                    <td className={c.to_review ? 'num text-warn' : 'num'}>{c.to_review}</td>
                    <td className="num muted">{dayMonth(c.updated_at)}</td>
                    <td className="row-actions" onClick={e => e.stopPropagation()}>
                      {showArchived ? (
                        <button type="button" className="link-btn" onClick={() => restore(c)}>Restore</button>
                      ) : (
                        <>
                          <button type="button" className="link-btn" onClick={() => setEditing({ client: c })}>Edit</button>
                          <button type="button" className="link-btn" onClick={() => archive(c)}>Archive</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
                {clients !== null && !shown.length && (
                  <tr>
                    <td colSpan={7} className="muted">
                      {showArchived ? 'No archived clients.' : 'No clients match your search.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
      {editing && <ClientDialog client={editing.client} onCancel={() => setEditing(null)} onSave={save} />}
      {confirmDialog}
    </>
  )
}
```

- [ ] **Step 8: Check types, tests and the build**

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected:
- no type errors;
- 14 tests pass;
- the build succeeds.

`next build` also switches `frontend/next-env.d.ts` to its build paths. Restore it with `git checkout -- frontend/next-env.d.ts`.

- [ ] **Step 9: Check it in the browser**

1. Add this entry to `configurations` in `.claude/launch.json`. The file is git-ignored. The entry runs an API on the spare port 8087 with a scratch database, so the user's own `data/ledgersync.db` is never touched:

```json
    {
      "name": "api-clients-8087",
      "runtimeExecutable": "/usr/bin/env",
      "runtimeArgs": [
        "LEDGERSYNC_PORT=8087",
        "LEDGERSYNC_DB_PATH=/private/tmp/claude-501/-Users-sandeepjala-ACTING-OFFICE--WORK-AI-Accountant---Temporary/53830d34-8d22-4ec2-9249-5f7189b1f02e/scratchpad/ledgersync-check.db",
        "GROQ_REASONING_EFFORT=low",
        "GROQ_MAX_IMAGES=1",
        "/Users/sandeepjala/ACTING OFFICE- WORK/AI-Accountant---Temporary/.venv/bin/python",
        "/Users/sandeepjala/ACTING OFFICE- WORK/AI-Accountant---Temporary/server.py"
      ],
      "port": 8087
    }
```

2. Start `api-clients-8087` and `web-dev-3001` with `preview_start`. `web-dev-3001` already points the UI at port 8087.
3. In the browser pane:
   - The page shows "Add your first client".
   - Add client with empty fields shows "Enter the company name" and "Enter the responsible person".
   - Add "Business Cube Ltd": limited company, Jenny Clarke, jenny@businesscube.co.uk. It is listed with "Registered".
   - Add a second client, "Harbour & Lane LLP": LLP, Matt Fisher, not VAT registered.
   - Searching "harbour" leaves one row.
   - Edit changes the phone.
   - Archive asks with the exact text; Archive hides the client, and the count reads "1 active · 1 archived".
   - Show archived, then Restore, brings it back.
   - The theme switch turns the page dark, and a reload keeps it dark.
   - The read console shows no errors.
4. Take a screenshot of the clients page in light mode and in dark mode.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/app/globals.css frontend/src/app/layout.tsx frontend/src/app/page.tsx frontend/src/lib/theme.ts frontend/src/lib/theme.test.ts frontend/src/components/AppHeader.tsx frontend/src/components/ConfirmDialog.tsx frontend/src/components/ClientDialog.tsx
git commit -m "Frontend: the Ledger look with a light/dark switch, and the clients page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Frontend — a client's page

Before writing code, read `frontend/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-params.md` and `03-file-conventions/dynamic-routes.md`.

**Files:**
- Create: `frontend/src/app/clients/[id]/page.tsx`
- Create: `frontend/src/components/TransactionsTable.tsx`, `UploadsList.tsx` and `TrialBalancePanel.tsx`

**Interfaces:**
- Consumes:
  - Task 11's calls and rules;
  - Task 12's `useConfirm`, `ClientDialog` and CSS;
  - `lib/ledger.ts` (`kindOf`, `money`, `rowStatus`, `totalsOf`);
  - `lib/duplicates.ts` (`possibleDuplicates`, `DUPLICATE_WINDOW_DAYS`).
- Produces:
  - `<TransactionsTable ledger readOnly onChange(rowId, change) onEdit?(row) />`. Revert shows on edited rows. The Edit button shows only when `onEdit` is passed, which Task 15 does.
  - `<UploadsList uploads readOnly onRemove(upload) />`.
  - `<TrialBalancePanel clientId version hasRows />`, which clears its result when `version` changes.
  - The client page, with `load()` (reload the ledger) and `show(ledger)`. Task 14 hands these to `AddDocuments`.

- [ ] **Step 1: The transactions table**

Create `frontend/src/components/TransactionsTable.tsx`:

```tsx
'use client'

import { useMemo } from 'react'
import type { Issue } from '@/lib/api'
import type { Ledger, RowChange, SavedRow } from '@/lib/clients'
import { DOCUMENT_TYPES } from '@/lib/clientRules'
import { DUPLICATE_WINDOW_DAYS, possibleDuplicates } from '@/lib/duplicates'
import { kindOf, money, rowStatus, totalsOf } from '@/lib/ledger'

// The worst issue on a row, as the Check column shows it.
function checkOf(issues: Issue[]) {
  if (!issues.length) return { mark: '✓', cls: 'check-ok', title: 'No issues' }
  const cls = issues.some(i => i.severity === 'error') ? 'check-error'
    : issues.some(i => i.severity === 'warning') ? 'check-warn' : 'check-info'
  return { mark: `${cls === 'check-error' ? '✖' : '⚠'} ${issues.length}`, cls, title: issues.map(i => i.message).join('\n') }
}

const FIELD_NAMES: Record<string, string> = {
  date: 'date', description: 'description', counterparty: 'counterparty', direction: 'in/out', gross: 'amount',
  vat: 'VAT', account_code: 'account', document_type: 'type',
}

// The hover on an edited row: what the model read, where it differs from now.
function originalText(row: SavedRow): string {
  const now = row as unknown as Record<string, string | null | undefined>
  const changed = Object.entries(row.original ?? {})
    .filter(([field, value]) => (value ?? '') !== (now[field] ?? ''))
    .map(([field, value]) => `${FIELD_NAMES[field] ?? field} ${field === 'gross' || field === 'vat' ? money(value) : value || 'none'}`)
  return changed.length ? `Read by the model: ${changed.join(', ')}` : 'Edited'
}

// The client's saved rows: statuses with Link, Unlink and Include, duplicate warnings, Revert, and totals.
export default function TransactionsTable({ ledger, readOnly, onChange, onEdit }: {
  ledger: Ledger
  readOnly: boolean
  onChange: (rowId: number, change: RowChange) => void
  onEdit?: (row: SavedRow) => void
}) {
  const rows = ledger.transactions
  const uploads = useMemo(() => new Map(ledger.uploads.map(u => [u.id, u.name])), [ledger.uploads])
  const duplicateOf = useMemo(() => possibleDuplicates(rows.map(row => ({
    sourceId: row.upload_id, gross: row.gross, direction: row.direction, date: row.date, kind: kindOf(row),
  }))), [rows])
  const totals = useMemo(() => totalsOf(rows), [rows])
  const duplicates = duplicateOf.filter(of => of !== null).length

  return (
    <section className="card section table-card">
      <div className="section-head">
        <h2>Transactions</h2>
        <span className="muted small">
          {rows.length} row{rows.length === 1 ? '' : 's'}
          {duplicates ? ` · ${duplicates} possible duplicate${duplicates === 1 ? '' : 's'}` : ''}
        </span>
      </div>
      {!rows.length ? <p className="muted">Add documents to see their transactions here.</p> : (
        <table className="table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Description</th>
              <th>In / out</th>
              <th className="num">Amount</th>
              <th className="num">VAT</th>
              <th className="num">Net</th>
              <th>Account</th>
              <th>Check</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const of = duplicateOf[i]
              const issues: Issue[] = of === null ? row.issues : [...row.issues, {
                code: 'possible_duplicate', severity: 'warning',
                message: `Possible duplicate of "${rows[of].description}" (${uploads.get(rows[of].upload_id) ?? 'another upload'}): `
                         + `same amount and direction, dated within ${DUPLICATE_WINDOW_DAYS} days.`,
              }]
              const check = checkOf(issues)
              const origin = `${uploads.get(row.upload_id) ?? ''} · ${DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? row.document_type}`
              return (
                <tr key={row.id} className={kindOf(row) === 'not booked' ? 'row-not-booked' : undefined}>
                  <td className="mono">{row.date ?? '—'}</td>
                  <td>
                    {row.description}
                    {row.edited && <span className="badge badge-muted edited" title={originalText(row)}>Edited</span>}
                    <div className="row-origin" title={origin}>{origin}</div>
                    {rowStatus(row).map((status, k) => (status.include !== undefined ? (
                      <label key={k} className="row-status">
                        <input type="checkbox" checked={status.include} disabled={readOnly}
                               onChange={e => onChange(row.id, { include: e.target.checked })} />
                        {status.text}
                      </label>
                    ) : (
                      <div key={k} className="row-status">
                        {status.text}
                        {!readOnly && status.actions.map((action, j) => (
                          <button key={j} type="button" className="link-btn" onClick={() => onChange(row.id, { link: action.link })}>
                            {action.label}
                          </button>
                        ))}
                      </div>
                    )))}
                  </td>
                  <td>
                    <span className={row.direction === 'in' ? 'badge badge-accent' : 'badge badge-muted'}>
                      {row.direction === 'in' ? 'Money in' : 'Money out'}
                    </span>
                  </td>
                  <td className="num">{money(row.gross)}</td>
                  <td className="num">{money(row.vat_posted)}</td>
                  <td className="num">{money(row.net)}</td>
                  <td>
                    {row.paid_against
                      ? `${row.paid_against} ${row.paid_against_name ?? ''}`
                      : `${row.account_code} ${row.account_name ?? ''}`}
                  </td>
                  <td className={`check ${check.cls}`} title={check.title}>{check.mark}</td>
                  <td className="row-actions">
                    {!readOnly && onEdit && <button type="button" className="link-btn" onClick={() => onEdit(row)}>Edit</button>}
                    {!readOnly && row.edited && (
                      <button type="button" className="link-btn" onClick={() => onChange(row.id, { revert: true })}>Revert</button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            {totals.map(t => (
              <tr key={t.key}>
                <td colSpan={3}>
                  {t.label} ({t.count} row{t.count === 1 ? '' : 's'})
                  {t.stillOwed != null && ` · ${money(t.stillOwed)} still owed`}
                </td>
                <td className="num">{money(t.gross)}</td>
                <td className="num" title={t.vat == null ? 'Fix the rows marked ✖ first' : undefined}>{money(t.vat)}</td>
                <td className="num" title={t.net == null ? 'Fix the rows marked ✖ first' : undefined}>{money(t.net)}</td>
                <td colSpan={3} />
              </tr>
            ))}
          </tfoot>
        </table>
      )}
    </section>
  )
}
```

- [ ] **Step 2: The uploads list and the trial balance**

Create `frontend/src/components/UploadsList.tsx`:

```tsx
'use client'

import type { Upload } from '@/lib/clients'
import { dayMonth, statementText } from '@/lib/clientRules'

// What was added to the client, newest first, with each bank statement's balance check and Remove.
export default function UploadsList({ uploads, readOnly, onRemove }: {
  uploads: Upload[]
  readOnly: boolean
  onRemove: (upload: Upload) => void
}) {
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Uploads</h2>
        <span className="muted small">{uploads.length}</span>
      </div>
      {!uploads.length ? <p className="muted small">Nothing added yet.</p> : (
        <ul className="upload-list">
          {uploads.map(upload => {
            const check = statementText(upload.statement)
            const status = upload.statement?.status
            return (
              <li key={upload.id}>
                <div className="upload-name" title={upload.name}>{upload.name}</div>
                <div className="muted small">{upload.rows} row{upload.rows === 1 ? '' : 's'} · {dayMonth(upload.created_at)}</div>
                {check && (
                  <div className={`small ${status === 'gap' ? 'text-warn' : status === 'ok' ? 'text-ok' : 'muted'}`}>{check}</div>
                )}
                {upload.warnings.length > 0 && (
                  <details className="small">
                    <summary>{upload.warnings.length} warning{upload.warnings.length === 1 ? '' : 's'}</summary>
                    {upload.warnings.map((warning, i) => <div key={i} className="muted">{warning}</div>)}
                  </details>
                )}
                {!readOnly && (
                  <button type="button" className="link-btn danger" onClick={() => onRemove(upload)}>Remove</button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
```

Create `frontend/src/components/TrialBalancePanel.tsx`:

```tsx
'use client'

import { useEffect, useState } from 'react'
import type { TrialBalanceResult } from '@/lib/api'
import { clientTrialBalance } from '@/lib/clients'
import { money } from '@/lib/ledger'

// The client's trial balance from its saved rows. A change to the rows clears one already shown.
export default function TrialBalancePanel({ clientId, version, hasRows }: {
  clientId: number
  version: number
  hasRows: boolean
}) {
  const [result, setResult] = useState<TrialBalanceResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { setResult(null); setError('') }, [version])

  const generate = async () => {
    setBusy(true)
    setError('')
    try {
      setResult(await clientTrialBalance(clientId))
    } catch (e) {
      setResult(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const apart = result ? Math.abs(Number(result.total_debits) - Number(result.total_credits)) : 0
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Trial balance</h2>
        <button type="button" className="btn" onClick={generate} disabled={busy || !hasRows}>
          {busy ? <><span className="spinner" aria-hidden="true" /> Generating…</> : 'Generate trial balance'}
        </button>
      </div>
      {!result && !error && (
        <p className="muted small">Built from every saved row that is booked. Rows marked ✖ need fixing first.</p>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      {result && (
        <>
          <table className="table">
            <thead><tr><th>Account</th><th className="num">Debit</th><th className="num">Credit</th></tr></thead>
            <tbody>
              {result.lines.map(line => (
                <tr key={line.code}>
                  <td><span className="mono">{line.code}</span> {line.name}</td>
                  <td className="num">{Number(line.debit) > 0 ? money(line.debit) : '—'}</td>
                  <td className="num">{Number(line.credit) > 0 ? money(line.credit) : '—'}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>Totals</td>
                <td className="num">{money(result.total_debits)}</td>
                <td className="num">{money(result.total_credits)}</td>
              </tr>
            </tfoot>
          </table>
          <span className={`badge tb-pill ${result.is_balanced ? 'badge-accent' : 'badge-warn'}`}>
            {result.is_balanced ? 'Debits equal credits' : `Out of balance by £${apart.toFixed(2)}`}
          </span>
        </>
      )}
    </section>
  )
}
```

- [ ] **Step 3: The client page**

Create `frontend/src/app/clients/[id]/page.tsx`:

```tsx
'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useState } from 'react'
import ClientDialog from '@/components/ClientDialog'
import { useConfirm } from '@/components/ConfirmDialog'
import TransactionsTable from '@/components/TransactionsTable'
import TrialBalancePanel from '@/components/TrialBalancePanel'
import UploadsList from '@/components/UploadsList'
import { ApiError } from '@/lib/api'
import {
  changeClient, changeRow, exportUrl, getLedger, removeUpload, type ClientFields, type Ledger, type RowChange, type Upload,
} from '@/lib/clients'
import { BUSINESS_TYPES, figuresOf } from '@/lib/clientRules'
import { money } from '@/lib/ledger'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// One client: its details, its figures, its saved transactions and uploads, and its trial balance.
export default function ClientPage() {
  const params = useParams<{ id: string }>()
  const clientId = Number(params.id)
  const { ask, dialog: confirmDialog } = useConfirm()
  const [ledger, setLedger] = useState<Ledger | null>(null)
  const [missing, setMissing] = useState(false)
  const [error, setError] = useState('')
  const [editingClient, setEditingClient] = useState(false)
  const [version, setVersion] = useState(0)   // bumped whenever the rows change: a trial balance shown is cleared

  const show = useCallback((next: Ledger) => {
    setLedger(next)
    setVersion(v => v + 1)
    setError('')
  }, [])

  const load = useCallback(async () => {
    if (!Number.isInteger(clientId) || clientId < 1) {
      setMissing(true)
      return
    }
    try {
      show(await getLedger(clientId))
    } catch (e) {
      if (e instanceof ApiError && e.code === 'not_found') setMissing(true)
      else setError(message(e))
    }
  }, [clientId, show])

  useEffect(() => { load() }, [load])

  // A change the API answers with the whole ledger, worked out again.
  const act = useCallback(async (change: () => Promise<Ledger>) => {
    try { show(await change()) } catch (e) { setError(message(e)) }
  }, [show])

  if (missing) {
    return (
      <div className="empty card">
        <h1>Client not found</h1>
        <p className="muted">It may never have existed, or the link is wrong.</p>
        <Link href="/" className="btn">Back to clients</Link>
      </div>
    )
  }
  if (!ledger) return error ? <div className="notice notice-error">{error}</div> : <p className="muted">Loading…</p>

  const { client } = ledger
  const figures = figuresOf(ledger.transactions)
  const contact = [client.contact_name, client.contact_email, client.contact_phone].filter(Boolean).join(' · ')

  const archive = async () => {
    if (!(await ask(`Archive ${client.name}? You can restore it from Show archived.`, 'Archive'))) return
    try { await changeClient(client.id, { archived: true }); await load() } catch (e) { setError(message(e)) }
  }
  const restore = async () => {
    try { await changeClient(client.id, { archived: false }); await load() } catch (e) { setError(message(e)) }
  }
  const saveDetails = async (fields: ClientFields) => {
    await changeClient(client.id, fields)
    setEditingClient(false)
    await load()
  }
  const remove = async (upload: Upload) => {
    const rows = `${upload.rows} row${upload.rows === 1 ? '' : 's'}`
    const question = `Remove ${upload.name} and its ${rows}? This can't be undone: analyse the file again to get them back.`
    if (await ask(question, 'Remove')) await act(() => removeUpload(client.id, upload.id))
  }
  const change = (rowId: number, rowChange: RowChange) => act(() => changeRow(client.id, rowId, rowChange))

  return (
    <>
      <nav className="crumbs"><Link href="/">Clients</Link> / {client.name}</nav>
      <div className="page-head">
        <div>
          <h1>
            {client.name}{' '}
            <span className={client.vat_registered ? 'badge badge-accent' : 'badge badge-warn'}>
              {client.vat_registered ? 'VAT registered' : 'Not VAT registered'}
            </span>
          </h1>
          <p className="muted">{BUSINESS_TYPES[client.business_type]} · {contact}</p>
        </div>
        <div className="head-actions">
          {!client.archived && <button type="button" className="btn" onClick={() => setEditingClient(true)}>Edit details</button>}
          <a className="btn" href={exportUrl(client.id)} download>Export to Excel</a>
          {!client.archived && <button type="button" className="btn btn-ghost" onClick={archive}>Archive</button>}
        </div>
      </div>
      {client.archived && (
        <div className="notice notice-warn">
          <span>Archived. Restore to add documents or make changes.</span>
          <button type="button" className="btn" onClick={restore}>Restore</button>
        </div>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      <div className="figures">
        <div className="card figure"><div className="figure-label">Rows</div><div className="figure-value">{figures.rows}</div></div>
        <div className="card figure">
          <div className="figure-label">To review</div>
          <div className={figures.toReview ? 'figure-value text-warn' : 'figure-value'}>{figures.toReview}</div>
        </div>
        <div className="card figure"><div className="figure-label">To pay</div><div className="figure-value">{money(figures.toPay)}</div></div>
        <div className="card figure"><div className="figure-label">To receive</div><div className="figure-value">{money(figures.toReceive)}</div></div>
      </div>
      <div className="workspace">
        <TransactionsTable ledger={ledger} readOnly={client.archived} onChange={change} />
        <UploadsList uploads={ledger.uploads} readOnly={client.archived} onRemove={remove} />
      </div>
      <TrialBalancePanel clientId={client.id} version={version} hasRows={ledger.transactions.length > 0} />
      {editingClient && <ClientDialog client={client} onCancel={() => setEditingClient(false)} onSave={saveDetails} />}
      {confirmDialog}
    </>
  )
}
```

- [ ] **Step 4: Check types, tests and the build**

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected:
- no type errors;
- 14 tests pass;
- the build lists the route `/clients/[id]`.

Then run `git checkout -- frontend/next-env.d.ts`.

- [ ] **Step 5: Check it in the browser**

1. With `api-clients-8087` and `web-dev-3001` running, give client 1 rows through the API: two BT bills, and a bank line that prints its balances.

```bash
curl -s -X POST http://127.0.0.1:8087/api/clients/1/uploads -H 'Content-Type: application/json' -d '{"name": "Manual entry", "kind": "manual", "transactions": [
 {"direction": "out", "gross": "72.00", "vat": "12.00", "account_code": "7502", "date": "2026-09-05", "description": "BT Business, broadband", "document_type": "invoice", "counterparty": "BT Business", "document_ref": "bt-0905"},
 {"direction": "out", "gross": "72.00", "vat": "12.00", "account_code": "7502", "date": "2026-09-20", "description": "BT Business, phone line", "document_type": "invoice", "counterparty": "BT Business", "document_ref": "bt-0920"},
 {"direction": "out", "gross": "72.00", "account_code": "7502", "date": "2026-09-25", "description": "BT GROUP PLC DD", "document_type": "statement", "counterparty": "BT GROUP PLC DD", "document_ref": "line-1", "balance": "928.00", "opening_balance": "1000.00", "closing_balance": "928.00"}
]}' | head -c 300
```

2. Open `http://localhost:3001/clients/1` and check:
   - the three rows show with their statuses;
   - To pay reads £144.00;
   - the upload says "Balances add up";
   - Link on the bank line makes one bill "Paid by bank line on 25 Sept", and To pay falls to £72.00;
   - Generate trial balance shows "Debits equal credits";
   - a JavaScript `fetch('/api/clients/1/export.xlsx')` returns 200 with a `content-disposition` naming `Business Cube Ltd`;
   - Archive asks first, then shows the banner and hides Remove and the Link buttons, while Export stays;
   - Restore brings everything back;
   - Remove asks with the exact text, and the rows go;
   - `/clients/999` shows "Client not found";
   - the read console shows no errors.

Take a screenshot of the client page.

- [ ] **Step 6: Commit**

```bash
git add "frontend/src/app/clients/[id]/page.tsx" frontend/src/components/TransactionsTable.tsx frontend/src/components/UploadsList.tsx frontend/src/components/TrialBalancePanel.tsx
git commit -m "Frontend: a client's page with its saved transactions, uploads, figures, trial balance and export

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Frontend — adding documents to a client

**Files:**
- Create: `frontend/src/components/AddDocuments.tsx`
- Modify: `frontend/src/app/clients/[id]/page.tsx` (render it for an active client)

**Interfaces:**
- Consumes:
  - from Task 9, `/api/analyze` with `client_id`;
  - from Task 11, `addManualRows`, `sameFileAs` and `dayMonth`;
  - `lib/api.ts`: `analyze`, `getHealth` and `ApiError`;
  - from `lib/duplicates.ts`, `fingerprint`.
- Produces: `<AddDocuments clientId uploads onSaved() onLedger(ledger) />`. It has three tabs:
  - **Upload files:** up to 20 files at a time, read `max_parallel_jobs` at a time. A file matching a saved upload, or picked twice in the same batch, is skipped.
  - **Paste text:** pasted text is analysed for the client.
  - **Add by hand:** the model suggests an account; the row is saved through `addManualRows`.

- [ ] **Step 1: The panel**

Create `frontend/src/components/AddDocuments.tsx`:

```tsx
'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { analyze, ApiError, getHealth, type Transaction } from '@/lib/api'
import { addManualRows, type Ledger, type Upload } from '@/lib/clients'
import { dayMonth, sameFileAs } from '@/lib/clientRules'
import { fingerprint } from '@/lib/duplicates'

const MAX_BATCH_FILES = 20

type FileStatus = 'waiting' | 'reading' | 'done' | 'failed' | 'skipped' | 'cancelled'

interface BatchItem {
  id: number
  file: File
  hash: string | null      // fingerprint of the file's content (null if the browser cannot hash)
  status: FileStatus
  detail: string
}

const MARK: Record<FileStatus, string> = { waiting: '○', reading: '', done: '✓', failed: '✖', skipped: '–', cancelled: '■' }

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

const form = (fields: Record<string, string | Blob>) => {
  const data = new FormData()
  for (const [key, value] of Object.entries(fields)) data.append(key, value)
  return data
}

// Upload, Paste and Add by hand for one client. Each analysis runs as a job that the API saves to the client
// when it finishes, so onSaved reloads the ledger; a row added by hand is saved here, once the model has
// suggested its account.
export default function AddDocuments({ clientId, uploads, onSaved, onLedger }: {
  clientId: number
  uploads: Upload[]
  onSaved: () => void
  onLedger: (ledger: Ledger) => void
}) {
  const [tab, setTab] = useState<'upload' | 'paste' | 'manual'>('upload')
  const [pasteText, setPasteText] = useState('')
  const [manual, setManual] = useState({ description: '', amount: '', direction: 'out' as 'in' | 'out' })
  const [dragOver, setDragOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  const [batch, setBatch] = useState<BatchItem[]>([])
  const [limits, setLimits] = useState({ parallel: 1, maxMb: 20 })
  const cancelRef = useRef(false)
  const nextId = useRef(1)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    getHealth()
      .then(h => setLimits({ parallel: Math.max(1, h.max_parallel_jobs ?? 1), maxMb: h.max_upload_mb }))
      .catch(() => { /* the header says the API is down; the defaults stand */ })
  }, [])

  const update = useCallback((id: number, patch: Partial<BatchItem>) =>
    setBatch(prev => prev.map(item => (item.id === id ? { ...item, ...patch } : item))), [])

  const analyseText = async () => {
    if (!pasteText.trim()) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    setProgress('Uploading…')
    try {
      const result = await analyze(form({ text: pasteText.trim(), client_id: String(clientId) }), setProgress,
                                   () => cancelRef.current)
      if (!result.transactions.length) throw new ApiError('No transactions were found in this text.', 'empty')
      setPasteText('')
      onSaved()
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
    }
  }

  // Reads the waiting files, up to `parallel` at a time; each is saved to the client as it finishes.
  const runBatch = async (items: BatchItem[]) => {
    const todo = items.filter(item => item.status === 'waiting')
    if (!todo.length) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    let next = 0
    let read = 0
    const worker = async () => {
      while (next < todo.length) {
        const item = todo[next++]
        if (cancelRef.current) {
          update(item.id, { status: 'cancelled', detail: 'Not read: cancelled' })
          continue
        }
        update(item.id, { status: 'reading', detail: 'Uploading…' })
        try {
          const result = await analyze(form({ file: item.file, client_id: String(clientId) }),
                                       p => update(item.id, { detail: p }), () => cancelRef.current)
          const count = result.transactions.length
          if (!count) throw new ApiError('No transactions were found in this file.', 'empty')
          update(item.id, { status: 'done', detail: `${count} row${count === 1 ? '' : 's'} saved` })
          onSaved()
        } catch (e) {
          const cancelled = e instanceof ApiError && e.code === 'cancelled'
          update(item.id, { status: cancelled ? 'cancelled' : 'failed', detail: message(e) })
        }
        read++
        setProgress(`${read} of ${todo.length} files read, up to ${limits.parallel} at a time`)
      }
    }
    setProgress(`Reading ${todo.length} file${todo.length === 1 ? '' : 's'}, up to ${limits.parallel} at a time…`)
    await Promise.all(Array.from({ length: Math.min(limits.parallel, todo.length) }, worker))
    setBusy(false)
    setProgress('')
  }

  const pickFiles = async (files: FileList | null) => {
    const picked = Array.from(files ?? [])
    if (fileInput.current) fileInput.current.value = ''   // lets the same files be chosen again
    if (!picked.length) return
    const hashes = await Promise.all(picked.slice(0, MAX_BATCH_FILES).map(fingerprint))
    const items: BatchItem[] = []
    hashes.forEach((hash, n) => {
      const file = picked[n]
      // A file already saved for this client, or picked twice, is not read again: its rows would be booked twice.
      const saved = sameFileAs(hash, uploads)
      const twice = hash ? items.find(item => item.hash === hash) : undefined
      // Size is checked here because the Next proxy cuts oversized bodies instead of refusing them.
      const tooBig = file.size > limits.maxMb * 1024 * 1024
      const detail = saved ? `Skipped: same file as ${saved.name}, uploaded ${dayMonth(saved.created_at)}`
        : twice ? `Skipped: same file as ${twice.file.name} in this upload`
        : tooBig ? `Larger than the ${limits.maxMb} MB limit: split it and upload the parts` : 'Waiting'
      items.push({ id: nextId.current++, file, hash, status: saved || twice || tooBig ? 'skipped' : 'waiting', detail })
    })
    setBatch(items)
    setError(picked.length > MAX_BATCH_FILES
      ? `Only the first ${MAX_BATCH_FILES} files were added; upload the other ${picked.length - MAX_BATCH_FILES} next.`
      : '')
    runBatch(items)
  }

  const retry = () => {
    const again = batch.map(item => (item.status === 'failed' || item.status === 'cancelled'
      ? { ...item, status: 'waiting' as const, detail: 'Waiting' } : item))
    setBatch(again)
    runBatch(again)
  }

  // A row typed by hand: the model only suggests its account; the ledger splits VAT like any other row's.
  const addByHand = async () => {
    const amount = Math.abs(Number(manual.amount))
    const description = manual.description.trim()
    if (!description || !Number.isFinite(amount) || amount <= 0) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    setProgress('Asking the AI model for an account…')
    try {
      const verb = manual.direction === 'in' ? 'received' : 'paid'
      const result = await analyze(form({ text: `${description} - ${verb} GBP ${amount}` }), setProgress,
                                   () => cancelRef.current)
      const suggested = result.transactions[0]
      const row: Transaction = {
        date: null, description, direction: manual.direction, gross: amount.toFixed(2), vat: null,
        vat_treatment: null, vat_posted: null, net: null, account_code: suggested?.account_code ?? '9998',
        contra_account_code: null, currency: 'GBP', source: 'manual', method: 'user', evidence: null,
        document_type: 'receipt',
        issues: suggested
          ? suggested.issues.filter(i => i.code === 'account_not_recognised')
          : [{ code: 'account_not_recognised', severity: 'warning', message: 'No account suggested; posted to Suspense.' }],
      }
      onLedger(await addManualRows(clientId, [row]))
      setManual(m => ({ ...m, description: '', amount: '' }))
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
    }
  }

  const canRetry = !busy && batch.some(item => item.status === 'failed' || item.status === 'cancelled')
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Add documents</h2>
        <span className="muted small">Invoices, receipts, bank statements and expense claims</span>
      </div>
      <div className="tabs" role="tablist">
        {(['upload', 'paste', 'manual'] as const).map(t => (
          <button key={t} type="button" role="tab" className="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t === 'upload' ? 'Upload files' : t === 'paste' ? 'Paste text' : 'Add by hand'}
          </button>
        ))}
      </div>
      {tab === 'upload' && (
        <>
          <input ref={fileInput} type="file" multiple hidden onChange={e => pickFiles(e.target.files)}
                 accept=".csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff" />
          <div className={`dropzone${dragOver ? ' drag-over' : ''}`} role="button" tabIndex={0}
               onClick={() => { if (!busy) fileInput.current?.click() }}
               onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && !busy) fileInput.current?.click() }}
               onDragOver={e => { e.preventDefault(); setDragOver(true) }}
               onDragLeave={() => setDragOver(false)}
               onDrop={e => { e.preventDefault(); setDragOver(false); if (!busy) pickFiles(e.dataTransfer.files) }}>
            <strong>Drop files here or click to choose</strong>
            Up to {MAX_BATCH_FILES} at a time: PDFs, photos, CSV and Excel files, up to {limits.maxMb} MB each
          </div>
        </>
      )}
      {tab === 'paste' && (
        <>
          <textarea className="input" value={pasteText} onChange={e => setPasteText(e.target.value)} aria-label="Text to analyse"
                    placeholder={'BT Business broadband, 05/09/2026, £72.00 including £12.00 VAT\nBank: 25/09/2026 BT GROUP PLC DD -72.00'} />
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={analyseText} disabled={busy || !pasteText.trim()}>
              Analyse
            </button>
          </div>
        </>
      )}
      {tab === 'manual' && (
        <>
          <div className="manual-grid">
            <label className="field">
              <span>Description</span>
              <input className="input" value={manual.description} placeholder="Office chair"
                     onChange={e => setManual(m => ({ ...m, description: e.target.value }))} />
            </label>
            <label className="field">
              <span>Amount (£)</span>
              <input className="input" type="number" step="0.01" min="0" value={manual.amount} placeholder="120.00"
                     onChange={e => setManual(m => ({ ...m, amount: e.target.value }))} />
            </label>
            <label className="field">
              <span>Money</span>
              <select className="input" value={manual.direction}
                      onChange={e => setManual(m => ({ ...m, direction: e.target.value as 'in' | 'out' }))}>
                <option value="out">Paid out</option>
                <option value="in">Received</option>
              </select>
            </label>
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={addByHand}
                    disabled={busy || !manual.description.trim() || !manual.amount}>
              Add row
            </button>
          </div>
        </>
      )}
      {busy && (
        <div className="progress">
          <span className="spinner" aria-hidden="true" />
          <span>{progress || 'Working…'}</span>
          <button type="button" className="btn btn-ghost" onClick={() => { cancelRef.current = true; setProgress('Cancelling…') }}>
            Cancel
          </button>
        </div>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      {batch.length > 0 && (
        <ul className="batch">
          {batch.map(item => (
            <li key={item.id} className={`batch-${item.status}`}>
              <span aria-hidden="true">{item.status === 'reading' ? <span className="spinner" /> : MARK[item.status]}</span>
              <span className="batch-name" title={item.file.name}>{item.file.name}</span>
              <span className="batch-detail" title={item.detail}>{item.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {canRetry && (
        <div className="form-actions"><button type="button" className="btn" onClick={retry}>Retry failed files</button></div>
      )}
    </section>
  )
}
```

- [ ] **Step 2: Show it on the client page**

In `frontend/src/app/clients/[id]/page.tsx`, add `import AddDocuments from '@/components/AddDocuments'`. Then add this line between the closing `</div>` of `figures` and `<div className="workspace">`:

```tsx
      {!client.archived && <AddDocuments clientId={client.id} uploads={ledger.uploads} onSaved={load} onLedger={show} />}
```

- [ ] **Step 3: Check types, tests and the build**

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected:
- no type errors;
- 14 tests pass;
- the build succeeds.

Then run `git checkout -- frontend/next-env.d.ts`.

- [ ] **Step 4: Check it in the browser, with a real analysis**

With `api-clients-8087` and `web-dev-3001` running, on `http://localhost:3001/clients/1`:

1. **Paste text.** Paste this and click Analyse:

   ```
   Purchase invoice INV-77 from Clearway Office Supplies, dated 08/09/2026: paper and toner 50.00 + VAT 10.00 = 60.00.
   ```

   The progress line moves, the rows appear with "Pasted text" as their upload, and a reload keeps them.
2. **Upload a file.** The browser pane can't open the file picker, so drive the hidden input from JavaScript:

   ```js
   const input = document.querySelector('input[type=file]')
   const data = new DataTransfer()
   data.items.add(new File(['Date,Description,Amount\n10/09/2026,CLEARWAY OFFICE SUPPLIES,-60.00\n'], 'clearway.csv', { type: 'text/csv' }))
   input.files = data.files
   input.dispatchEvent(new Event('change', { bubbles: true }))
   ```

   `clearway.csv` is read and saved. Run the same snippet again: the batch line reads "Skipped: same file as clearway.csv, uploaded {today}".
3. **Add by hand.** Add "Office chair", 120.00, Paid out. A "Manual entry" upload appears with one row.
4. **Console.** The read console shows no errors.

Take a screenshot.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AddDocuments.tsx "frontend/src/app/clients/[id]/page.tsx"
git commit -m "Frontend: add documents to a client by upload, paste or by hand; saved as each analysis finishes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Frontend — editing a row

**Files:**
- Create: `frontend/src/components/EditRowDialog.tsx`
- Modify: `frontend/src/app/clients/[id]/page.tsx` (Edit opens the dialog, and the saved change shows the new ledger)

**Interfaces:**
- Consumes:
  - from Task 7, `PATCH /rows/{id}` with the edit fields;
  - from Task 8, `/api/accounts?business_type=`;
  - from Task 11, `listAccounts`, `draftOf`, `rowEditErrors`, `changesOf` and `DOCUMENT_TYPES`;
  - from Task 13, `TransactionsTable`'s `onEdit`.
- Produces: `<EditRowDialog row businessType onCancel onSave(change) />`.

- [ ] **Step 1: The dialog**

Create `frontend/src/components/EditRowDialog.tsx`:

```tsx
'use client'

import { useEffect, useState, type FormEvent } from 'react'
import { listAccounts, type AccountChoice, type BusinessType, type RowChange, type SavedRow } from '@/lib/clients'
import { changesOf, DOCUMENT_TYPES, draftOf, rowEditErrors, type EditDraft, type EditErrors } from '@/lib/clientRules'

// Corrects what the model read on one row. Counterparty and document type change for the whole document.
export default function EditRowDialog({ row, businessType, onCancel, onSave }: {
  row: SavedRow
  businessType: BusinessType
  onCancel: () => void
  onSave: (change: RowChange) => Promise<void>
}) {
  const [draft, setDraft] = useState<EditDraft>(() => draftOf(row))
  const [accounts, setAccounts] = useState<AccountChoice[]>([])
  const [showErrors, setShowErrors] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const errors: EditErrors = rowEditErrors(draft)

  useEffect(() => {
    listAccounts(businessType).then(setAccounts).catch(e => setError(e instanceof Error ? e.message : String(e)))
  }, [businessType])

  const set = (field: keyof EditDraft, value: string) => setDraft(d => ({ ...d, [field]: value }))
  const fieldError = (field: keyof EditErrors) =>
    (showErrors && errors[field] ? <span className="field-error">{errors[field]}</span> : null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setShowErrors(true)
    if (Object.keys(errors).length) return
    const change = changesOf(row, draft)
    if (!Object.keys(change).length) {
      onCancel()
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSave(change)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  // The saved account stays listed even when this kind of business can't choose it, so the form shows the truth.
  const options = accounts.some(a => a.code === draft.account_code)
    ? accounts : [{ code: row.account_code, name: row.account_name ?? '', type: '', vat: '' }, ...accounts]

  return (
    <div className="overlay" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) onCancel() }}
         onKeyDown={e => { if (e.key === 'Escape') onCancel() }}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="edit-row-title" onSubmit={submit} noValidate>
        <h2 id="edit-row-title">Edit row</h2>
        <p className="dialog-note">Counterparty and document type change for every row of this document.</p>
        <div className="field-row">
          <label className="field">
            <span>Date</span>
            <input className="input" type="date" value={draft.date} onChange={e => set('date', e.target.value)} />
            {fieldError('date')}
          </label>
          <label className="field">
            <span>Document type</span>
            <select className="input" value={draft.document_type} onChange={e => set('document_type', e.target.value)}>
              {Object.entries(DOCUMENT_TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <label className="field">
          <span>Description</span>
          <input className="input" value={draft.description} onChange={e => set('description', e.target.value)} />
          {fieldError('description')}
        </label>
        <label className="field">
          <span>Counterparty</span>
          <input className="input" value={draft.counterparty} onChange={e => set('counterparty', e.target.value)}
                 placeholder="Who was paid, or who paid" />
        </label>
        <div className="field-row">
          <label className="field">
            <span>Money</span>
            <select className="input" value={draft.direction} onChange={e => set('direction', e.target.value)}>
              <option value="out">Paid out</option>
              <option value="in">Received</option>
            </select>
          </label>
          <label className="field">
            <span>Amount (£)</span>
            <input className="input" inputMode="decimal" value={draft.gross} onChange={e => set('gross', e.target.value)} />
            {fieldError('gross')}
          </label>
        </div>
        <div className="field-row">
          <label className="field">
            <span>VAT shown (£)</span>
            <input className="input" inputMode="decimal" value={draft.vat} onChange={e => set('vat', e.target.value)}
                   placeholder="None shown" />
            {fieldError('vat')}
          </label>
          <label className="field">
            <span>Account</span>
            <select className="input" value={draft.account_code} onChange={e => set('account_code', e.target.value)}>
              {options.map(a => <option key={a.code} value={a.code}>{a.code} {a.name}</option>)}
            </select>
          </label>
        </div>
        {error && <div className="notice notice-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </div>
  )
}
```

- [ ] **Step 2: Open it from the table**

In `frontend/src/app/clients/[id]/page.tsx`:

1. Add `import EditRowDialog from '@/components/EditRowDialog'`, and add `type SavedRow` to the import from `@/lib/clients`.
2. Next to the other `useState` calls, before the early returns, add:

```tsx
  const [editingRow, setEditingRow] = useState<SavedRow | null>(null)
```

3. After `const change = ...`, add:

```tsx
  const saveRow = async (rowChange: RowChange) => {
    if (!editingRow) return
    show(await changeRow(client.id, editingRow.id, rowChange))
    setEditingRow(null)
  }
```

4. Pass `onEdit={setEditingRow}` to `<TransactionsTable … />`.
5. After the `ClientDialog` line, add:

```tsx
      {editingRow && (
        <EditRowDialog row={editingRow} businessType={client.business_type}
                       onCancel={() => setEditingRow(null)} onSave={saveRow} />
      )}
```

- [ ] **Step 3: Check types, tests and the build**

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected:
- no type errors;
- 14 tests pass;
- the build succeeds.

Then run `git checkout -- frontend/next-env.d.ts`.

- [ ] **Step 4: Check it in the browser**

On `http://localhost:3001/clients/1`:

1. Edit the Clearway invoice row:
   - set the amount to 0: the form shows "Enter an amount above zero";
   - set the account to 7504 Office Stationery and save;
   - the row shows "Edited", hovering shows "Read by the model: account …", and the figures and totals update.
2. Revert: the row goes back and "Edited" disappears.
3. Edit a bank line's document type to Invoice and save: it becomes owed ("Unpaid, owed to …"). Revert it.
4. The read console shows no errors.

Take a screenshot with the dialog open.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/EditRowDialog.tsx "frontend/src/app/clients/[id]/page.tsx"
git commit -m "Frontend: edit what the model read on a row, and revert it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: README, and the whole flow checked

**Files:**
- Modify: `README.md`

- [ ] **Step 1: The README**

In `README.md`, under `## Run`, replace the paragraph that starts "The UI keeps one table across uploads" with:

```markdown
Each client's page keeps one table across its uploads and catches what was entered twice. A file already
uploaded for that client, on any day, or picked twice, is skipped without being read. A row with the same
amount and direction as a row from another upload, dated within three days of it (a receipt and its bank
line, say), is flagged "possible duplicate" and left for a person to decide.
Without a key or an internet connection the API answers 503 with how to fix it; it never guesses.
```

After the `### Unpaid bills, claims and payments` section, and before `## The AI model (Groq)`, add:

```markdown
### Clients and saved work

The first page lists your clients. Add one with its company name, its business type (limited company,
sole trader, partnership or LLP), its responsible person (the client's own contact, with email and phone)
and whether it is VAT registered. Open a client to add its documents: each analysis is saved to that
client in a local database when it finishes, with the decisions you make (Link, Include, edits), so
closing the browser loses nothing. Archive hides a client and keeps everything; Restore brings it back.

- Edit corrects a row: date, description, counterparty, money in or out, amount, VAT shown, account and
  document type. The row shows "Edited", and Revert puts back what the model read.
- A bank statement is checked against the balances it prints. A line the model missed or misread shows
  where the balance breaks, and the upload says "Doesn't add up".
- Export to Excel downloads the client's trial balance and transactions as one workbook.
- VAT on client entertainment (7403) and on cars (0050 Cars) is not reclaimed; vans (0055 Vans) are. A
  limited company's owners use 2250 Director's Loan Account; other businesses use 3260 Drawings and
  3000 Capital Introduced.

The database is one SQLite file, `data/ledgersync.db` (git-ignored), or wherever `LEDGERSYNC_DB_PATH`
points; deleting it removes every client. The endpoints are under `/api/clients`, and the design is in
`docs/superpowers/specs/2026-10-06-clients-and-local-database-design.md`.
```

In the configuration table, after the `LEDGERSYNC_LOG_LEVEL` row, add:

```markdown
| `LEDGERSYNC_DB_PATH` | `data/ledgersync.db` | The local database of clients and their saved rows |
```

- [ ] **Step 2: Run everything**

Run: `.venv/bin/python -m pytest -q`
Expected: no failures.

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected: no type errors, 14 tests pass, and the build succeeds. Then run `git checkout -- frontend/next-env.d.ts`.

- [ ] **Step 3: The whole flow in the browser**

1. Stop the servers and delete the scratch database file and its `-wal` and `-shm` files. They live in the scratchpad, never in the project.
2. Start `api-clients-8087` and `web-dev-3001` again.
3. Go through the flow, checking each step:
   - add "Northbridge Consulting Ltd" (limited company, VAT registered);
   - paste the Clearway invoice from Task 14, and a bank line that pays it;
   - reload: everything is still there;
   - the bank line pays the invoice, and To pay is £0.00;
   - edit a row, then revert it;
   - the statement upload shows its balance check;
   - generate the trial balance;
   - export to Excel (the fetch returns 200);
   - archive the client: it is read-only, and the clients page shows it under Show archived;
   - restore it;
   - switch to dark mode;
   - the read console shows no errors.
4. Take screenshots of the clients page and the client page.
5. Stop both servers.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "README: clients and saved work

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
