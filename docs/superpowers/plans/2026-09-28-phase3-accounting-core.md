# Phase 3 — Accounting Core (Double-Entry + VAT): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-sided trial balance with real double-entry bookkeeping:
- a UK chart of accounts;
- exact money with the VAT split out;
- every transaction posted against the bank.

The trial balance then balances by construction, and the API and UI switch to the new transaction shape once.

**Architecture:** Five small modules in `ledgersync/`:
- `money` (Decimal, VAT arithmetic);
- `accounts` (the chart);
- `models` (Pydantic API shapes);
- `checks` (normalise a transaction: VAT, bank account, issues);
- `posting` (journal lines and trial balance).

A temporary `adapter` maps the Phase 1 model's rows onto the new `Transaction`, and Phase 4 deletes it. The Phase 1 row validation stops rejecting negative amounts: the sign becomes the direction.

**Tech Stack:** Python 3.11, Decimal, Pydantic v2, FastAPI; Next.js 16 for the minimal UI update; the Phase 2 eval to measure.

**Spec:** `docs/superpowers/specs/2026-09-28-ledgersync-robustness-design.md` (Accounting core, Phase 3)

## Global Constraints

- **Money:** `Decimal` everywhere and a string in JSON (`"12.50"`). No float arithmetic. VAT rounds half up to the penny.
- **Posting:**
  - `out` → Dr account (net), Dr VAT (vat), Cr bank (gross).
  - `in` → Dr bank (gross), Cr account (net), Cr VAT (vat).
  - The VAT leg goes to 2200 for income accounts and to 2201 otherwise.
  - Every journal balances (asserted).
- **VAT:**
  - Not VAT-registered → vat 0, net = gross.
  - VAT shown on the document wins.
  - Otherwise VAT is estimated from the account's default (standard = gross/6, reduced = gross/21) and flagged `vat_estimated`.
  - VAT never applies to liability or equity accounts.
- **Accounts:** every `account_code` must exist in `accounts.CHART`, and every code used by `eval/fixtures` must exist.
- **Issues:** error-level issues block the trial balance (422 `transactions_need_fixing`). Warnings and info never change amounts.
- **Field names:** the Pydantic field named `date` needs `import datetime as dt` and `Optional[dt.date]`, never `date: date`.
- **Tests:** `.venv/bin/python -m pytest`. Frontend: `npx tsc --noEmit`, `npm run build`, and a browser check.

## Review Focus

1. **A transaction with estimated VAT comes back for validation** (UI round trip). It stays flagged as estimated and is not mistaken for document VAT, with no duplicate issues. Test: `test_renormalising_keeps_the_estimate_flag_without_duplicates` (Task 3).
2. **The model returns a negative amount** (bank rows). The row becomes money out instead of being dropped, which fixes the baseline's bank recall. Test: `test_negative_amounts_become_money_out` (Task 5).
3. **A transaction uses an account that isn't in the chart** (typo). The trial balance refuses with a 422 naming the row, and nothing is half-posted. Test: `test_unknown_account_blocks_the_trial_balance` (Task 4), `test_trial_balance_rejects_unpostable_rows_with_422` (Task 6).
4. **Money arrives as a float with binary noise** (0.1 + 0.2). It becomes exact pennies. Test: `test_float_noise_becomes_exact_pennies` (Task 3).
5. **A purchase and its refund cancel out.** No zero lines appear and the trial balance still balances. Test: `test_cancelling_transactions_leave_no_zero_lines` (Task 4).

---

### Task 1: Money and VAT arithmetic

**Files:** Create `ledgersync/money.py`; Test `tests/test_money.py`

**Interfaces — Produces:**
- `PENNY`, `ZERO`
- `VatTreatment` (str Enum: `standard`, `reduced`, `zero`, `exempt`, `outside_scope`)
- `to_money(value) -> Optional[Decimal]`
- `vat_in_gross(gross: Decimal, treatment: VatTreatment) -> Decimal`

- [ ] **Step 1: Failing tests** — `tests/test_money.py`:

```python
from decimal import Decimal

import pytest

from ledgersync.money import VatTreatment as V, to_money, vat_in_gross


@pytest.mark.parametrize("gross, treatment, vat", [
    ("120.00", V.STANDARD, "20.00"),
    ("7.50", V.STANDARD, "1.25"),
    ("68.39", V.STANDARD, "11.40"),       # 11.398 rounds up
    ("21.00", V.REDUCED, "1.00"),
    ("12.50", V.ZERO, "0.00"),
    ("45.00", V.EXEMPT, "0.00"),
    ("1450.00", V.OUTSIDE_SCOPE, "0.00"),
    ("0.03", V.STANDARD, "0.01"),         # exactly half a penny: half up (banker's rounding gives 0.00)
])
def test_vat_contained_in_a_gross_amount(gross, treatment, vat):
    assert vat_in_gross(Decimal(gross), treatment) == Decimal(vat)


@pytest.mark.parametrize("raw, value", [
    ("£1,234.50", "1234.50"), (72.1, "72.10"), ("0.005", "0.01"), ("-72", "-72.00"),
    ("abc", None), ("", None), (None, None), ("nan", None), (True, None),
])
def test_to_money_reads_exact_pennies(raw, value):
    assert to_money(raw) == (Decimal(value) if value is not None else None)
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_money.py -q`. Expected: `ModuleNotFoundError: No module named 'ledgersync.money'`.

- [ ] **Step 3: Implement** — `ledgersync/money.py`:

```python
"""Money and VAT arithmetic: exact pennies, rounded half up, VAT taken out of gross amounts."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Optional

PENNY = Decimal("0.01")
ZERO = Decimal("0.00")


class VatTreatment(str, Enum):
    STANDARD = "standard"            # 20%
    REDUCED = "reduced"              # 5%
    ZERO = "zero"                    # 0%: VAT-able, at a nil rate
    EXEMPT = "exempt"
    OUTSIDE_SCOPE = "outside_scope"


_RATES = {VatTreatment.STANDARD: Decimal("0.20"), VatTreatment.REDUCED: Decimal("0.05")}


def to_money(value) -> Optional[Decimal]:
    """Pennies from a number or text such as '£1,234.50'; None when it is not an amount.
    Floats go through str() so 72.1 stays 72.10, and 0.1 + 0.2 becomes 0.30."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace("£", "").replace(",", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(PENNY, rounding=ROUND_HALF_UP)


def vat_in_gross(gross: Decimal, treatment: VatTreatment) -> Decimal:
    """VAT inside a VAT-inclusive amount: gross/6 at 20%, gross/21 at 5%, otherwise none."""
    rate = _RATES.get(treatment)
    if rate is None:
        return ZERO
    return (gross * rate / (1 + rate)).quantize(PENNY, rounding=ROUND_HALF_UP)
```

- [ ] **Step 4: Run and confirm pass.** Run the same command. Expected: 17 passed.
- [ ] **Step 5: Commit** — `git add ledgersync/money.py tests/test_money.py && git commit -m "Add exact money and VAT arithmetic"`

---

### Task 2: UK chart of accounts

**Files:** Create `ledgersync/accounts.py`; Test `tests/test_accounts.py`

**Interfaces:**
- Consumes: `VatTreatment` (Task 1).
- Produces:
  - `AccountType` (str Enum: asset, liability, equity, income, expense)
  - `Account(code, name, type, vat, aliases)`
  - `CHART: tuple[Account, ...]` and `BY_CODE: dict[str, Account]`
  - Constants `BANK="1200"`, `SALES_VAT="2200"`, `PURCHASE_VAT="2201"`, `SUSPENSE="9998"`
  - `match_name(text) -> Optional[str]`

- [ ] **Step 1: Failing tests** — `tests/test_accounts.py`:

```python
import json
from pathlib import Path

import pytest

from ledgersync.accounts import BY_CODE, CHART, AccountType, match_name

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_codes_are_unique_four_digit_strings():
    codes = [a.code for a in CHART]
    assert len(codes) == len(set(codes)) and all(len(c) == 4 and c.isdigit() for c in codes)


def test_every_code_the_eval_expects_exists():
    used = {r["account_code"] for p in FIXTURES.glob("*/expected.json") for r in json.loads(p.read_text())["rows"]}
    assert used and used <= set(BY_CODE)


def test_posting_accounts_have_the_right_types():
    assert BY_CODE["1200"].type == AccountType.ASSET
    assert BY_CODE["2200"].type == BY_CODE["2201"].type == AccountType.LIABILITY
    assert BY_CODE["4000"].type == AccountType.INCOME and BY_CODE["7502"].type == AccountType.EXPENSE


@pytest.mark.parametrize("text, code", [
    ("Telephone & Internet", "7502"), ("Utilities", "7200"), ("Sales", "4000"), ("Revenue", "4000"),
    ("VAT", "2202"), ("Office Equipment", "0030"), ("Travel Expenses", "7400"),
    ("Staff Refreshments", "8205"), ("Professional Fees", "7603"),
    ("General Expenses", None), ("", None), (None, None),
])
def test_phase1_account_names_map_to_codes(text, code):
    assert match_name(text) == code
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_accounts.py -q`. Expected: `ModuleNotFoundError: No module named 'ledgersync.accounts'`.

- [ ] **Step 3: Implement** — `ledgersync/accounts.py`:

```python
"""UK chart of accounts (Sage 50-style nominal codes). Every transaction posts to one of these,
and the AI picks from this list instead of inventing account names."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .money import VatTreatment as V


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    type: AccountType
    vat: V                          # default VAT when the document does not show it
    aliases: tuple[str, ...] = ()   # names a model may use for this account


_A, _L, _E, _I, _X = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
                      AccountType.INCOME, AccountType.EXPENSE)

CHART: tuple[Account, ...] = (
    Account("0030", "Office Equipment", _A, V.STANDARD, ("computer equipment", "equipment")),
    Account("0040", "Furniture and Fixtures", _A, V.STANDARD, ("furniture",)),
    Account("0050", "Motor Vehicles", _A, V.STANDARD, ("vehicles",)),
    Account("1200", "Bank Current Account", _A, V.OUTSIDE_SCOPE, ("bank",)),
    Account("1210", "Bank Deposit Account", _A, V.OUTSIDE_SCOPE, ("savings", "deposit account")),
    Account("1230", "Petty Cash", _A, V.OUTSIDE_SCOPE, ("cash",)),
    Account("2200", "Sales VAT", _L, V.OUTSIDE_SCOPE, ("output vat",)),
    Account("2201", "Purchase VAT", _L, V.OUTSIDE_SCOPE, ("input vat",)),
    Account("2202", "VAT Liability", _L, V.OUTSIDE_SCOPE, ("vat", "hmrc vat", "vat payment")),
    Account("2210", "PAYE and National Insurance", _L, V.OUTSIDE_SCOPE, ("paye", "national insurance", "nic")),
    Account("2300", "Loans", _L, V.OUTSIDE_SCOPE, ("loan", "loans")),
    Account("3000", "Capital Introduced", _E, V.OUTSIDE_SCOPE, ("capital",)),
    Account("3260", "Drawings", _E, V.OUTSIDE_SCOPE, ("drawings",)),
    Account("4000", "Sales", _I, V.STANDARD, ("revenue", "income", "sales revenue", "turnover")),
    Account("4900", "Other Income", _I, V.STANDARD, ("miscellaneous income", "interest received")),
    Account("5000", "Purchases", _X, V.STANDARD, ("cost of goods sold", "cost of sales", "stock")),
    Account("7000", "Gross Wages", _X, V.OUTSIDE_SCOPE, ("wages", "salaries", "payroll")),
    Account("7100", "Rent", _X, V.STANDARD, ("office rent", "desk rental")),
    Account("7103", "Business Rates", _X, V.OUTSIDE_SCOPE, ("rates",)),
    Account("7200", "Electricity", _X, V.STANDARD, ("utilities", "energy")),
    Account("7201", "Gas", _X, V.STANDARD, ()),
    Account("7300", "Fuel and Oil", _X, V.STANDARD, ("fuel", "petrol", "diesel", "motor expenses")),
    Account("7302", "Vehicle Licences", _X, V.OUTSIDE_SCOPE, ("vehicle tax", "road tax")),
    Account("7400", "Travel", _X, V.ZERO, ("travelling", "travel expenses", "transport", "train", "taxi")),
    Account("7402", "Hotels", _X, V.STANDARD, ("hotel", "accommodation")),
    Account("7403", "Entertainment", _X, V.STANDARD, ("client entertainment",)),
    Account("7406", "Subsistence", _X, V.STANDARD, ("meals", "food")),
    Account("7500", "Printing", _X, V.STANDARD, ()),
    Account("7501", "Postage and Carriage", _X, V.EXEMPT, ("postage", "courier", "delivery")),
    Account("7502", "Telephone and Internet", _X, V.STANDARD, ("telephone", "internet", "broadband", "mobile", "phone")),
    Account("7504", "Office Stationery", _X, V.STANDARD, ("stationery", "office supplies", "office expenses")),
    Account("7600", "Legal Fees", _X, V.STANDARD, ("legal", "filing fees")),
    Account("7601", "Accountancy Fees", _X, V.STANDARD, ("accountancy", "accounting fees", "audit")),
    Account("7602", "Consultancy Fees", _X, V.STANDARD, ("contractors",)),
    Account("7603", "Professional Fees", _X, V.STANDARD, ()),
    Account("7700", "Equipment Hire", _X, V.STANDARD, ("hire",)),
    Account("7800", "Repairs and Renewals", _X, V.STANDARD, ("repairs", "maintenance")),
    Account("7801", "Cleaning", _X, V.STANDARD, ()),
    Account("7900", "Bank Interest Paid", _X, V.EXEMPT, ("interest paid", "bank interest")),
    Account("7901", "Bank Charges", _X, V.EXEMPT, ("bank fees", "card fees")),
    Account("8200", "Donations", _X, V.OUTSIDE_SCOPE, ("charity",)),
    Account("8201", "Subscriptions and Software", _X, V.STANDARD, ("subscriptions", "software")),
    Account("8203", "Training", _X, V.STANDARD, ("courses",)),
    Account("8204", "Insurance", _X, V.EXEMPT, ()),
    Account("8205", "Refreshments", _X, V.ZERO, ("staff refreshments", "groceries")),
    Account("9998", "Suspense", _L, V.OUTSIDE_SCOPE, ()),
)

BY_CODE: dict[str, Account] = {a.code: a for a in CHART}
BANK, SALES_VAT, PURCHASE_VAT, SUSPENSE = "1200", "2200", "2201", "9998"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("&", " and ")).split())


_NAMES: dict[str, str] = {}
for _account in CHART:
    for _name in (_account.name, *_account.aliases):
        _NAMES.setdefault(_norm(_name), _account.code)
_BY_LENGTH = sorted(_NAMES.items(), key=lambda item: -len(item[0].split()))


def match_name(text: Optional[str]) -> Optional[str]:
    """Code for a free-text account name as the Phase 1 model writes it, or None. Exact names
    and aliases first, then the longest alias whose words all appear in the text."""
    if not text or not _norm(text):
        return None
    key = _norm(text)
    if key in _NAMES:
        return _NAMES[key]
    words = set(key.split())
    for name, code in _BY_LENGTH:
        if set(name.split()) <= words:
            return code
    return None
```

- [ ] **Step 4: Run and confirm pass.** Run the same command. Expected: 15 passed.
- [ ] **Step 5: Commit** — `git add ledgersync/accounts.py tests/test_accounts.py && git commit -m "Add UK chart of accounts"`

---

### Task 3: Models and normalisation (VAT split, bank account, issues)

**Files:**
- Create `ledgersync/models.py`, `ledgersync/checks.py`
- Modify `ledgersync/errors.py` (add `InvalidTransactions`)
- Test `tests/test_checks.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `Direction` (`in`/`out`) and `Issue(code, message, severity)`
  - `BusinessSettings(business_name="", vat_registered=True, period_start=None, period_end=None, bank_account="1200")`
  - `Transaction`, with fields `date, description, direction, gross, vat, vat_treatment, net, account_code, contra_account_code, currency, source, method, evidence, issues` and the computed `account_name`
  - `TrialBalanceLine(code, name, type, debit, credit)`, `JournalLine(transaction, code, debit, credit, description)`, `TrialBalance(lines, total_debits, total_credits, is_balanced, journal)`
  - `LedgerRequest(transactions, settings)`, `TransactionList(transactions)`, `AnalysisResult(transactions, warnings, model)`
  - `checks.normalise(tx, settings) -> Transaction`
  - `errors.InvalidTransactions` (422 `transactions_need_fixing`)

- [ ] **Step 1: Failing tests** — `tests/test_checks.py`:

```python
import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ledgersync.checks import normalise
from ledgersync.models import BusinessSettings, Transaction

REGISTERED = BusinessSettings()
NOT_REGISTERED = BusinessSettings(vat_registered=False)


def tx(direction="out", gross="120.00", account="7502", **kw):
    return Transaction(direction=direction, gross=gross, account_code=account,
                       date=kw.pop("date", dt.date(2026, 9, 1)), description="t", **kw)


def codes(t):
    return [(i.code, i.severity) for i in t.issues]


def test_vat_is_estimated_from_the_account_default_and_flagged():
    t = normalise(tx(), REGISTERED)
    assert (t.vat, t.net, t.contra_account_code) == (Decimal("20.00"), Decimal("100.00"), "1200")
    assert codes(t) == [("vat_estimated", "warning")]


def test_vat_shown_on_the_document_wins():
    t = normalise(tx(gross="7.95", account="8205", vat="0.70"), REGISTERED)
    assert (t.vat, t.net, codes(t)) == (Decimal("0.70"), Decimal("7.25"), [])


def test_not_vat_registered_books_the_gross():
    t = normalise(tx(), NOT_REGISTERED)
    assert (t.vat, t.net, codes(t)) == (Decimal("0.00"), Decimal("120.00"), [])


def test_vat_never_applies_to_liabilities():
    t = normalise(tx(gross="1450.00", account="2202", vat="10.00"), REGISTERED)
    assert t.vat == Decimal("0.00") and codes(t) == [("vat_not_applicable", "warning")]


@pytest.mark.parametrize("change, code", [
    ({"account": "9999"}, "unknown_account"),
    ({"account": "1200"}, "same_account"),
    ({"currency": "usd"}, "non_gbp_currency"),
    ({"vat": "130.00"}, "vat_arithmetic"),
])
def test_unpostable_rows_get_an_error(change, code):
    assert (code, "error") in codes(normalise(tx(**change), REGISTERED))


def test_dates_are_checked_against_the_period():
    period = BusinessSettings(period_start=dt.date(2026, 9, 1), period_end=dt.date(2026, 9, 30))
    assert ("date_missing", "warning") in codes(normalise(tx(date=None), period))
    assert ("date_out_of_period", "warning") in codes(normalise(tx(date=dt.date(2026, 10, 2)), period))


def test_money_in_on_an_expense_account_is_flagged_as_a_likely_refund():
    assert ("unusual_direction", "info") in codes(normalise(tx(direction="in"), REGISTERED))


def test_odd_vat_on_a_standard_rated_account_is_flagged():
    assert ("vat_rate_mismatch", "info") in codes(normalise(tx(vat="5.00"), REGISTERED))


def test_renormalising_keeps_the_estimate_flag_without_duplicates():
    once = normalise(tx(), REGISTERED)
    twice = normalise(Transaction.model_validate(once.model_dump(mode="json")), REGISTERED)
    assert codes(twice) == [("vat_estimated", "warning")] and twice.vat == Decimal("20.00")


def test_float_noise_becomes_exact_pennies():
    assert tx(gross=0.1 + 0.2).gross == Decimal("0.30")


def test_a_pound_sign_or_no_currency_means_gbp():
    assert tx(currency="£").currency == tx(currency=None).currency == "GBP"


@pytest.mark.parametrize("gross", ["0", "-5.00", "abc"])
def test_gross_must_be_a_positive_amount(gross):
    with pytest.raises(ValidationError):
        tx(gross=gross)


def test_account_name_is_included_in_json():
    assert tx().model_dump(mode="json")["account_name"] == "Telephone and Internet"
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_checks.py -q`. Expected: `ModuleNotFoundError: No module named 'ledgersync.checks'`.

- [ ] **Step 3: Implement.**

`ledgersync/errors.py`: append

```python
class InvalidTransactions(LedgerSyncError):
    status_code = 422
    code = "transactions_need_fixing"
```

`ledgersync/models.py`:

```python
"""API and ledger models. Money is Decimal inside and a string in JSON ("12.50")."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field, field_validator

from .accounts import BANK, BY_CODE
from .money import VatTreatment, to_money


class Direction(str, Enum):
    IN = "in"      # money into the bank
    OUT = "out"    # money out of the bank


class Issue(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"]


class BusinessSettings(BaseModel):
    business_name: str = ""
    vat_registered: bool = True
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    bank_account: str = BANK


class Transaction(BaseModel):
    date: Optional[dt.date] = None
    description: str = ""
    direction: Direction
    gross: Decimal
    vat: Optional[Decimal] = None                  # as shown on the document; None = not shown
    vat_treatment: Optional[VatTreatment] = None   # None = the account's default
    net: Optional[Decimal] = None                  # filled in by checks.normalise
    account_code: str
    contra_account_code: Optional[str] = None      # None = the business's bank account
    currency: str = "GBP"
    source: str = "text"
    method: Literal["parsed", "llm", "vlm", "user"] = "llm"
    evidence: Optional[str] = None
    issues: list[Issue] = Field(default_factory=list)

    @field_validator("gross", mode="before")
    @classmethod
    def _positive_pennies(cls, value):
        money = to_money(value)
        if money is None or money <= 0:
            raise ValueError("gross must be a positive amount; direction says whether money went in or out")
        return money

    @field_validator("vat", "net", mode="before")
    @classmethod
    def _pennies(cls, value):
        if value is None:
            return None
        money = to_money(value)
        if money is None:
            raise ValueError("not an amount")
        return money

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_code(cls, value):
        code = str(value or "").strip().upper()
        return "GBP" if code in ("", "£") else code

    @computed_field
    @property
    def account_name(self) -> Optional[str]:
        account = BY_CODE.get(self.account_code)
        return account.name if account else None


class TransactionList(BaseModel):
    transactions: list[Transaction]


class AnalysisResult(TransactionList):
    warnings: list[str] = Field(default_factory=list)
    model: Optional[str] = None


class LedgerRequest(BaseModel):
    transactions: list[Transaction]
    settings: BusinessSettings = Field(default_factory=BusinessSettings)


class JournalLine(BaseModel):
    transaction: int     # position in the request
    code: str
    debit: Decimal
    credit: Decimal
    description: str


class TrialBalanceLine(BaseModel):
    code: str
    name: str
    type: str
    debit: Decimal
    credit: Decimal


class TrialBalance(BaseModel):
    lines: list[TrialBalanceLine]
    total_debits: Decimal
    total_credits: Decimal
    is_balanced: bool
    journal: list[JournalLine]
```

`ledgersync/checks.py`:

```python
"""Makes a transaction ready to post (VAT split, bank account) and lists what a bookkeeper
should look at. Issues never change amounts; error-level issues block the trial balance."""
from __future__ import annotations

from decimal import Decimal

from .accounts import BY_CODE, AccountType
from .models import BusinessSettings, Direction, Issue, Transaction
from .money import ZERO, VatTreatment, vat_in_gross

_NO_VAT = {AccountType.LIABILITY, AccountType.EQUITY}
_DERIVED = {"unknown_account", "same_account", "non_gbp_currency", "vat_not_applicable", "vat_estimated",
            "vat_arithmetic", "vat_rate_mismatch", "date_missing", "date_out_of_period", "unusual_direction"}


def _issue(code: str, message: str, severity: str = "warning") -> Issue:
    return Issue(code=code, message=message, severity=severity)


def normalise(tx: Transaction, settings: BusinessSettings) -> Transaction:
    kept = [i for i in tx.issues if i.code not in _DERIVED]   # re-validating must not duplicate
    issues: list[Issue] = []
    account = BY_CODE.get(tx.account_code)
    contra = tx.contra_account_code or settings.bank_account
    if account is None:
        issues.append(_issue("unknown_account", f"Account {tx.account_code} is not in the chart of accounts.", "error"))
    if contra not in BY_CODE:
        issues.append(_issue("unknown_account", f"Account {contra} is not in the chart of accounts.", "error"))
    if contra == tx.account_code:
        issues.append(_issue("same_account", "A transaction cannot post to and from the same account.", "error"))
    if tx.currency != "GBP":
        issues.append(_issue("non_gbp_currency", f"{tx.currency} amounts must be converted to GBP first.", "error"))

    treatment = tx.vat_treatment or (account.vat if account else VatTreatment.OUTSIDE_SCOPE)
    # An estimate from an earlier pass is not document VAT: estimate it again.
    vat = None if any(i.code == "vat_estimated" for i in tx.issues) else tx.vat
    if not settings.vat_registered:
        vat = ZERO
    elif account is not None and account.type in _NO_VAT:
        if vat:
            issues.append(_issue("vat_not_applicable", f"VAT does not apply to {account.name}; it was ignored."))
        vat, treatment = ZERO, VatTreatment.OUTSIDE_SCOPE
    elif vat is None:
        vat = vat_in_gross(tx.gross, treatment)
        if vat > 0:
            issues.append(_issue("vat_estimated", f"VAT of £{vat} estimated at the {treatment.value} rate; "
                                                  "check it against the invoice."))
    elif vat < 0 or vat >= tx.gross:
        issues.append(_issue("vat_arithmetic", f"VAT £{vat} cannot be negative or reach the amount £{tx.gross}.",
                             "error"))
    elif (treatment in (VatTreatment.STANDARD, VatTreatment.REDUCED)
          and abs(vat - vat_in_gross(tx.gross, treatment)) > Decimal("0.02")):
        issues.append(_issue("vat_rate_mismatch", f"£{vat} is not {treatment.value}-rate VAT on £{tx.gross}; "
                                                  "mixed rates?", "info"))

    if tx.date is None:
        issues.append(_issue("date_missing", "No date found; add the transaction date."))
    elif ((settings.period_start and tx.date < settings.period_start)
          or (settings.period_end and tx.date > settings.period_end)):
        issues.append(_issue("date_out_of_period", f"{tx.date} is outside the accounting period."))
    if account is not None and tx.direction == Direction.IN and account.type == AccountType.EXPENSE:
        issues.append(_issue("unusual_direction", f"Money in on {account.name} is usually a refund; check it.", "info"))
    if account is not None and tx.direction == Direction.OUT and account.type == AccountType.INCOME:
        issues.append(_issue("unusual_direction", f"Money out on {account.name} is usually a customer refund; "
                                                  "check it.", "info"))
    net = tx.gross - vat if vat is not None and vat < tx.gross else None
    return tx.model_copy(update={"vat": vat, "net": net, "vat_treatment": treatment,
                                 "contra_account_code": contra, "issues": kept + issues})
```

- [ ] **Step 4: Run and confirm pass.** Run the same command. Expected: 18 passed.
- [ ] **Step 5: Commit** — `git add ledgersync/models.py ledgersync/checks.py ledgersync/errors.py tests/test_checks.py && git commit -m "Add ledger models and transaction normalisation with issues"`

---

### Task 4: Double-entry posting and trial balance

**Files:** Create `ledgersync/posting.py`; Test `tests/test_posting.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `journal_for(tx: Transaction, index: int) -> list[JournalLine]` (the transaction must already be normalised)
  - `trial_balance(transactions: list[Transaction], settings: BusinessSettings) -> TrialBalance` (raises `InvalidTransactions`)

- [ ] **Step 1: Failing tests** — `tests/test_posting.py`:

```python
import datetime as dt
import random

import pytest

from ledgersync import accounts
from ledgersync.checks import normalise
from ledgersync.errors import InvalidTransactions
from ledgersync.models import BusinessSettings, Transaction
from ledgersync.posting import journal_for, trial_balance

REGISTERED = BusinessSettings()


def tx(direction, gross, account, **kw):
    return Transaction(direction=direction, gross=gross, account_code=account, description="t",
                       date=dt.date(2026, 9, 1), **kw)


def lines(t, settings=REGISTERED):
    return [(l.code, str(l.debit), str(l.credit)) for l in journal_for(normalise(t, settings), 0)]


@pytest.mark.parametrize("name, t, expected", [
    ("expense with VAT", tx("out", "120.00", "7502"),
     [("7502", "100.00", "0.00"), ("2201", "20.00", "0.00"), ("1200", "0.00", "120.00")]),
    ("sale with document VAT", tx("in", "2400.00", "4000", vat="400.00"),
     [("4000", "0.00", "2000.00"), ("2200", "0.00", "400.00"), ("1200", "2400.00", "0.00")]),
    ("supplier refund", tx("in", "89.99", "0030"),
     [("0030", "0.00", "74.99"), ("2201", "0.00", "15.00"), ("1200", "89.99", "0.00")]),
    ("customer refund", tx("out", "120.00", "4000"),
     [("4000", "100.00", "0.00"), ("2200", "20.00", "0.00"), ("1200", "0.00", "120.00")]),
    ("HMRC VAT payment", tx("out", "1450.00", "2202"), [("2202", "1450.00", "0.00"), ("1200", "0.00", "1450.00")]),
    ("PAYE and NIC", tx("out", "2140.37", "2210"), [("2210", "2140.37", "0.00"), ("1200", "0.00", "2140.37")]),
    ("drawings", tx("out", "500.00", "3260"), [("3260", "500.00", "0.00"), ("1200", "0.00", "500.00")]),
    ("loan received", tx("in", "10000.00", "2300"), [("2300", "0.00", "10000.00"), ("1200", "10000.00", "0.00")]),
    ("transfer to savings", tx("out", "5000.00", "1210"), [("1210", "5000.00", "0.00"), ("1200", "0.00", "5000.00")]),
    ("mixed-VAT receipt", tx("out", "7.95", "8205", vat="0.70"),
     [("8205", "7.25", "0.00"), ("2201", "0.70", "0.00"), ("1200", "0.00", "7.95")]),
])
def test_postings(name, t, expected):
    assert lines(t) == expected


def test_not_vat_registered_posts_the_gross():
    assert lines(tx("out", "120.00", "7502"), BusinessSettings(vat_registered=False)) == [
        ("7502", "120.00", "0.00"), ("1200", "0.00", "120.00")]


def test_trial_balance_shows_net_balances_by_code():
    tb = trial_balance([tx("out", "120.00", "7502"), tx("in", "2400.00", "4000", vat="400.00")], REGISTERED)
    assert [(l.code, l.name, str(l.debit), str(l.credit)) for l in tb.lines] == [
        ("1200", "Bank Current Account", "2280.00", "0.00"),
        ("2200", "Sales VAT", "0.00", "400.00"),
        ("2201", "Purchase VAT", "20.00", "0.00"),
        ("4000", "Sales", "0.00", "2000.00"),
        ("7502", "Telephone and Internet", "100.00", "0.00"),
    ]
    assert (str(tb.total_debits), str(tb.total_credits), tb.is_balanced) == ("2400.00", "2400.00", True)


def test_cancelling_transactions_leave_no_zero_lines():
    tb = trial_balance([tx("out", "120.00", "7502"), tx("in", "120.00", "7502")], REGISTERED)
    assert tb.lines == [] and tb.is_balanced and len(tb.journal) == 6


def test_unknown_account_blocks_the_trial_balance():
    with pytest.raises(InvalidTransactions, match="#2") as info:
        trial_balance([tx("out", "10.00", "7502"), tx("out", "10.00", "9999")], REGISTERED)
    assert "9999" in info.value.message


def test_trial_balance_always_balances_for_random_ledgers():
    rng = random.Random(3)
    codes = [a.code for a in accounts.CHART if a.code != accounts.BANK]
    ledger = []
    for n in range(1000):
        gross = f"{rng.randrange(1, 10**6) / 100:.2f}"
        extra = {"vat": f"{float(gross) * rng.choice([0, 0.05, 0.1, 0.16]):.2f}"} if n % 4 == 0 else {}
        ledger.append(tx(rng.choice(["in", "out"]), gross, rng.choice(codes), **extra))
    tb = trial_balance(ledger, REGISTERED)
    assert tb.is_balanced and tb.total_debits == tb.total_credits
    for n in range(len(ledger)):
        mine = [l for l in tb.journal if l.transaction == n]
        assert sum(l.debit for l in mine) == sum(l.credit for l in mine)
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_posting.py -q`. Expected: `ModuleNotFoundError: No module named 'ledgersync.posting'`.

- [ ] **Step 3: Implement** — `ledgersync/posting.py`:

```python
"""Double entry: each transaction becomes balanced journal lines against the bank (or its
contra account), with VAT on its own control account; the trial balance sums them."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .accounts import BY_CODE, PURCHASE_VAT, SALES_VAT, AccountType
from .checks import normalise
from .errors import InvalidTransactions
from .models import BusinessSettings, Direction, JournalLine, Transaction, TrialBalance, TrialBalanceLine
from .money import ZERO


def journal_for(tx: Transaction, index: int) -> list[JournalLine]:
    """Journal lines for one normalised transaction: account and VAT legs, then the bank leg."""
    account = BY_CODE[tx.account_code]
    vat_account = SALES_VAT if account.type == AccountType.INCOME else PURCHASE_VAT
    out = tx.direction == Direction.OUT
    lines = [JournalLine(transaction=index, code=code, description=tx.description,
                         debit=amount if out else ZERO, credit=ZERO if out else amount)
             for code, amount in ((tx.account_code, tx.net), (vat_account, tx.vat)) if amount]
    lines.append(JournalLine(transaction=index, code=tx.contra_account_code, description=tx.description,
                             debit=ZERO if out else tx.gross, credit=tx.gross if out else ZERO))
    debits, credits = sum(l.debit for l in lines), sum(l.credit for l in lines)
    if debits != credits:   # net + vat == gross by construction; this guards the invariant
        raise AssertionError(f"transaction {index} does not balance: {debits} != {credits}")
    return lines


def trial_balance(transactions: list[Transaction], settings: BusinessSettings) -> TrialBalance:
    ready = [normalise(tx, settings) for tx in transactions]
    problems = [f"#{n + 1}: {issue.message}" for n, tx in enumerate(ready)
                for issue in tx.issues if issue.severity == "error"]
    if problems:
        raise InvalidTransactions(f"{len(problems)} problem(s) must be fixed before the trial balance: "
                                  + " ".join(problems[:5]))
    journal = [line for n, tx in enumerate(ready) for line in journal_for(tx, n)]
    balances: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in journal:
        balances[line.code] += line.debit - line.credit
    lines = [TrialBalanceLine(code=code, name=BY_CODE[code].name, type=BY_CODE[code].type.value,
                              debit=balance if balance > 0 else ZERO, credit=-balance if balance < 0 else ZERO)
             for code, balance in sorted(balances.items()) if balance != 0]
    total_debits = sum((l.debit for l in lines), ZERO)
    total_credits = sum((l.credit for l in lines), ZERO)
    return TrialBalance(lines=lines, total_debits=total_debits, total_credits=total_credits,
                        is_balanced=total_debits == total_credits, journal=journal)
```

- [ ] **Step 4: Run and confirm pass.** Run the same command. Expected: 15 passed.
- [ ] **Step 5: Commit** — `git add ledgersync/posting.py tests/test_posting.py && git commit -m "Add double-entry posting and a trial balance that balances by construction"`

---

### Task 5: Analysis results in the new shape (temporary adapter)

**Files:**
- Create `ledgersync/adapter.py`; Test `tests/test_adapter.py`
- Modify `qwen_service.py` (drop `ge=0`), `server.py` (analysis job), and `legacy/app.js` (read the new shape)
- Update tests: `tests/test_qwen_service.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `adapter.parse_date(text) -> Optional[dt.date]`
  - `adapter.to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]`
  - The analysis job result becomes `AnalysisResult` JSON: `{"transactions": [...], "warnings": [...], "model": ...}`

- [ ] **Step 1: Failing tests** — `tests/test_adapter.py`:

```python
import datetime as dt
from decimal import Decimal

from ledgersync.adapter import parse_date, to_transactions
from ledgersync.models import BusinessSettings

ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0,
       "currency": "GBP", "type": "expense", "account": "Telephone & Internet"}


def adapt(*rows):
    return to_transactions(list(rows), source="table", settings=BusinessSettings())


def test_model_rows_become_ledger_transactions():
    [t] = adapt(ROW)
    assert (t.direction.value, t.gross, t.account_code, t.date) == ("out", Decimal("72.00"), "7502", dt.date(2026, 9, 1))
    assert (t.vat, t.source, t.method) == (Decimal("12.00"), "table", "llm")


def test_negative_amounts_become_money_out():
    # Baseline: bank rows the model kept negative were dropped, so bank exports scored 20%.
    [t] = adapt({**ROW, "amount": -72.0, "type": "revenue"})
    assert t.direction.value == "out" and t.gross == Decimal("72.00")
    assert "direction_conflict" in [i.code for i in t.issues]


def test_unknown_account_names_go_to_suspense_with_a_warning():
    [t] = adapt({**ROW, "account": "General Expenses"})
    assert t.account_code == "9998" and ("account_not_recognised", "warning") in [(i.code, i.severity) for i in t.issues]


def test_the_bank_account_itself_is_not_a_category():
    # "Bank Transfer" matches the bank; posting the bank against itself is impossible.
    [t] = adapt({**ROW, "account": "Bank Transfer"})
    assert t.account_code == "9998" and not [i for i in t.issues if i.severity == "error"]


def test_zero_or_unreadable_amounts_are_skipped():
    assert adapt({**ROW, "amount": 0}, {**ROW, "amount": None}) == []


def test_uk_dates_are_read_day_first():
    assert parse_date("03/09/2026") == dt.date(2026, 9, 3)
    assert parse_date("2026-09-03 00:00:00") == dt.date(2026, 9, 3)
    assert parse_date("sometime") is None
```

In `tests/test_qwen_service.py`, a negative amount is now valid, so replace the first "bad row" in `test_one_bad_row_does_not_fail_the_batch`:

```python
def test_one_bad_row_does_not_fail_the_batch():
    no_amount = {k: v for k, v in ROW.items() if k != "amount"}
    result, _ = extract(transactions_json(no_amount, ROW, dict(ROW, type="transfer")))
    assert result.count == 1 and len(result.warnings) == 2
    assert result.warnings[0].startswith("Row 1 skipped (amount:")
    assert result.warnings[1].startswith("Row 3 skipped (type:")


def test_negative_amounts_are_kept_for_the_ledger_to_read_as_money_out():
    result, _ = extract(transactions_json(dict(ROW, amount=-72.0)))
    assert result.count == 1 and result.data[0].amount == -72.0
```

In `tests/test_api.py`, update the two tests that read the result:

```python
def test_analyze_text_runs_as_job(make_client):
    job = run_text_job(make_client())
    assert job["status"] == "succeeded" and job["result"]["model"] == "fake-model"
    [t] = job["result"]["transactions"]
    assert (t["direction"], t["gross"], t["account_code"], t["account_name"]) == (
        "out", "72.00", "7502", "Telephone and Internet")


def test_partial_success_returns_valid_rows_and_warnings(make_client):
    job = run_text_job(make_client(FakeOllama([transactions_json(dict(ROW, type="transfer"), ROW)])))
    assert len(job["result"]["transactions"]) == 1 and len(job["result"]["warnings"]) == 1
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_adapter.py tests/test_qwen_service.py tests/test_api.py -q`. Expected:
  - `test_adapter.py` fails on the missing module.
  - The negative-amount qwen test fails (`count == 0`).
  - The two API tests fail with `KeyError: 'transactions'`.

- [ ] **Step 3: Implement.**

`ledgersync/adapter.py`:

```python
"""Temporary (Phase 3): maps the Phase 1 model's rows onto ledger Transactions until Phase 4
replaces extraction. A negative amount means money out, whatever the model called it."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .accounts import SUSPENSE, match_name
from .checks import normalise
from .models import BusinessSettings, Direction, Issue, Transaction
from .money import to_money


def parse_date(text) -> Optional[dt.date]:
    """ISO or UK day-first dates; None when absent or unreadable."""
    if not text:
        return None
    raw = str(text).strip()
    for candidate, fmt in ((raw[:10], "%Y-%m-%d"), (raw, "%d/%m/%Y"), (raw, "%d/%m/%y"), (raw, "%d-%m-%Y")):
        try:
            return dt.datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]:
    result = []
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        direction = Direction.OUT if amount < 0 or row.get("type") == "expense" else Direction.IN
        if amount < 0 and row.get("type") == "revenue":
            issues.append(Issue(code="direction_conflict", severity="warning",
                                message="The model called this income but the amount was negative; recorded as money out."))
        code = match_name(row.get("account"))
        if code is None or code == settings.bank_account:
            code = SUSPENSE
            issues.append(Issue(code="account_not_recognised", severity="warning",
                                message=f"No account to post '{row.get('account')}' to; it went to Suspense for review."))
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=direction, gross=abs(amount), account_code=code,
                         currency=row.get("currency") or "GBP", source=source, method="llm", issues=issues)
        result.append(normalise(tx, settings))
    return result
```

`qwen_service.py`: in `AccountingTransaction`, change

```python
    amount: float = Field(..., ge=0, description="Numeric transaction amount")
```

to

```python
    amount: float = Field(..., description="Numeric transaction amount")   # negative = money out (ledger adapter)
```

`server.py`:
- Imports: add `from ledgersync import adapter` and `from ledgersync.models import AnalysisResult, BusinessSettings`.
- Inside `create_app`, add before `analyze_transaction`:

```python
    def run_analysis(item, ctx) -> dict:
        extraction = pipeline.analyze(item, extractor, ocr, ctx)
        transactions = adapter.to_transactions([row.model_dump() for row in extraction.data],
                                               source=item.kind, settings=BusinessSettings())
        return AnalysisResult(transactions=transactions, warnings=extraction.warnings,
                              model=extraction.model).model_dump(mode="json")
```

- Change the submit line to `job = jobs.submit(lambda ctx: run_analysis(item, ctx))`.

`legacy/app.js`: in `analyzeViaJob`, change `if (job.status === 'succeeded') return job.result;` to `if (job.status === 'succeeded') return toLegacyResult(job.result);`, and add above `analyzeViaJob`:

```js
// The API now returns ledger transactions; this retiring page still reads the Phase 1 shape.
function toLegacyResult(result) {
  const data = (result.transactions || []).map(t => ({
    description: t.description, date: t.date, amount: Number(t.gross),
    type: t.direction === 'out' ? 'expense' : 'revenue', account: t.account_name || t.account_code,
  }));
  return { success: true, data };
}
```

- [ ] **Step 4: Run and confirm pass.** Run the Step 2 command, then `node --check legacy/app.js`. Expected: all pass, and the syntax check succeeds.
- [ ] **Step 5: Commit** — `git add ledgersync/adapter.py qwen_service.py server.py legacy/app.js tests/test_adapter.py tests/test_qwen_service.py tests/test_api.py && git commit -m "Return analyses as ledger transactions; negative amounts become money out"`

---

### Task 6: Ledger endpoints (accounts, validate, trial balance v2)

**Files:** Modify `server.py`; Test `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `GET /api/accounts` → `[{code, name, type, vat}]`
  - `POST /api/transactions/validate` with `{transactions, settings?}` → `{transactions: [normalised]}`
  - `POST /api/trial-balance` with `{transactions, settings?}` → `TrialBalance`, or 422 `transactions_need_fixing`

- [ ] **Step 1: Failing tests** — in `tests/test_api.py`, replace `test_trial_balance_still_works` with:

```python
BT = {"direction": "out", "gross": "72.00", "account_code": "7502", "date": "2026-09-01", "description": "BT"}


def test_accounts_are_listed(make_client):
    accounts = make_client().get("/api/accounts").json()
    assert {"code": "7502", "name": "Telephone and Internet", "type": "expense", "vat": "standard"} in accounts


def test_validate_splits_vat_and_flags_the_estimate(make_client):
    [t] = make_client().post("/api/transactions/validate", json={"transactions": [BT]}).json()["transactions"]
    assert (t["vat"], t["net"], t["contra_account_code"]) == ("12.00", "60.00", "1200")
    assert [i["code"] for i in t["issues"]] == ["vat_estimated"]


def test_trial_balance_balances_with_bank_and_vat_legs(make_client):
    sale = {**BT, "direction": "in", "gross": "240.00", "account_code": "4000", "vat": "40.00"}
    tb = make_client().post("/api/trial-balance", json={"transactions": [BT, sale]}).json()
    assert tb["is_balanced"] is True and tb["total_debits"] == tb["total_credits"] == "240.00"
    assert [(l["code"], l["debit"], l["credit"]) for l in tb["lines"]] == [
        ("1200", "168.00", "0.00"), ("2200", "0.00", "40.00"), ("2201", "12.00", "0.00"),
        ("4000", "0.00", "200.00"), ("7502", "60.00", "0.00")]


def test_trial_balance_rejects_unpostable_rows_with_422(make_client):
    resp = make_client().post("/api/trial-balance", json={"transactions": [BT, {**BT, "account_code": "9999"}]})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "transactions_need_fixing"
    assert "#2" in resp.json()["detail"]["message"]


def test_non_vat_registered_business_gets_gross_postings(make_client):
    tb = make_client().post("/api/trial-balance",
                            json={"transactions": [BT], "settings": {"vat_registered": False}}).json()
    assert [(l["code"], l["debit"], l["credit"]) for l in tb["lines"]] == [
        ("1200", "0.00", "72.00"), ("7502", "72.00", "0.00")]
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_api.py -q`. Expected: the five new tests fail (404, or 422 from the old endpoint's body shape).

- [ ] **Step 3: Implement** in `server.py`:
- Remove the `TrialBalanceLine` / `TrialBalanceResult` classes, the old `generate_trial_balance`, and the now-unused imports (`Dict`, `List`, `BaseModel`, `AccountingTransaction`).
- Imports become:

```python
from ledgersync import accounts, adapter, intake, pipeline, posting
from ledgersync.checks import normalise
from ledgersync.models import AnalysisResult, BusinessSettings, LedgerRequest, TransactionList, TrialBalance
from qwen_service import QwenAccountingExtractor
```

- Add in place of the old trial-balance route:

```python
    # ─── Ledger ─────────────────────────────────────────────────────────────────

    @app.get("/api/accounts")
    def list_accounts():
        return [{"code": a.code, "name": a.name, "type": a.type.value, "vat": a.vat.value} for a in accounts.CHART]

    @app.post("/api/transactions/validate", response_model=TransactionList)
    def validate_transactions(request: LedgerRequest):
        """Splits VAT, fills in the bank account and lists issues; no posting."""
        return TransactionList(transactions=[normalise(tx, request.settings) for tx in request.transactions])

    @app.post("/api/trial-balance", response_model=TrialBalance)
    def generate_trial_balance(request: LedgerRequest):
        """Double-entry trial balance; 422 when a transaction cannot be posted."""
        return posting.trial_balance(request.transactions, request.settings)
```

- [ ] **Step 4: Run and confirm pass.** Run `.venv/bin/python -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `git add server.py tests/test_api.py && git commit -m "Add accounts, validate and double-entry trial balance endpoints"`

---

### Task 7: Eval posts the new trial-balance request

**Files:** Modify `eval/run_eval.py`; Test `tests/test_eval_runner.py`

- [ ] **Step 1: Failing test.** In `test_case_is_run_through_the_api_and_scored`, replace `assert outcome["tb_balanced"] in (True, False)` with:

```python
    assert outcome["tb_balanced"] is True                 # double entry balances by construction
    assert outcome["checks"]["account"] == [1, 1]         # "VAT" maps to 2202, as expected
```

Add:

```python
def test_rows_that_cannot_be_posted_count_as_no_trial_balance(api):
    # Skipping a refused trial balance would hide it from the metric.
    case = load_cases(FIXTURES, "text-hmrc-vat-payment")[0]
    outcome = run_case(api(FakeOllama([transactions_json(dict(HMRC_ROW, currency="USD"))])), case, timeout=10)
    assert outcome["tb_balanced"] is False
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_eval_runner.py -q -k "run_through or cannot_be_posted"`. Expected: both FAIL with `tb_balanced` None: the old bare-list body gets a validation 422.

- [ ] **Step 3: Implement.** In `eval/run_eval.py`, replace the body of `_trial_balance_ok` with:

```python
    if result.get("transactions") is not None:   # Phase 3+: {transactions, settings}
        rows, body = result["transactions"], {"transactions": result["transactions"]}
    else:                                        # Phase 1: a bare list of rows
        rows = body = result.get("data")
    if not rows:
        return None
    try:
        resp = _call(client.post, "/api/trial-balance", json=body)
    except ApiUnreachable:
        return None
    if resp.status_code == 422 and _error_code(resp) == "transactions_need_fixing":
        return False                             # the rows cannot be posted as they are
    return bool(resp.json().get("is_balanced")) if resp.status_code == 200 else None
```

- [ ] **Step 4: Run and confirm pass.** Run `.venv/bin/python -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `git add eval/run_eval.py tests/test_eval_runner.py && git commit -m "Eval: post the double-entry trial-balance request"`

---

### Task 8: Minimal frontend update

**Files:** Modify `frontend/src/lib/api.ts`, `frontend/src/app/page.tsx`, `frontend/src/app/globals.css`

- [ ] **Step 1: `api.ts` types.** Replace the `AccountingTransaction`, `AnalyzeResult`, `TrialBalanceLine` and `TrialBalanceResult` interfaces with:

```ts
export interface Issue {
  code: string
  message: string
  severity: 'info' | 'warning' | 'error'
}

export interface Transaction {
  date: string | null
  description: string
  direction: 'in' | 'out'
  gross: string
  vat: string | null
  vat_treatment: string | null
  net: string | null
  account_code: string
  account_name?: string | null
  contra_account_code: string | null
  currency: string
  source: string
  method: string
  evidence: string | null
  issues: Issue[]
}

export interface AnalyzeResult {
  transactions: Transaction[]
  warnings: string[]
  model: string | null
}

export interface TrialBalanceLine {
  code: string
  name: string
  type: string
  debit: string
  credit: string
}

export interface TrialBalanceResult {
  lines: TrialBalanceLine[]
  total_debits: string
  total_credits: string
  is_balanced: boolean
}
```

Add, next to `trialBalance`, the validate call (the backend owns the VAT maths):

```ts
export function validateTransactions(transactions: Transaction[]): Promise<{ transactions: Transaction[] }> {
  return request<{ transactions: Transaction[] }>('/api/transactions/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transactions }),
  })
}
```

Then change `trialBalance` to send the new body:

```ts
export function trialBalance(transactions: Transaction[]): Promise<TrialBalanceResult> {
  return request<TrialBalanceResult>('/api/trial-balance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transactions }),
  })
}
```

- [ ] **Step 2: `page.tsx`.** Make these edits:
  1. In the import list, `type AccountingTransaction,` becomes `type Transaction,`, and add `validateTransactions,` after `trialBalance,`.
  2. `useState<AccountingTransaction[]>([])` becomes `useState<Transaction[]>([])`.
  3. In `runAnalysis`, the parameter `toTransactions: (result: AnalyzeResult) => AccountingTransaction[] = result => result.data` becomes `toTransactions: (result: AnalyzeResult) => Transaction[] | Promise<Transaction[]> = result => result.transactions`, and `const rows = toTransactions(result)` becomes `const rows = await toTransactions(result)`.
  4. Add below `fmt`:

```tsx
const money = (value: string | null) => (value == null ? '—' : fmt(Number(value)) || '£0.00')

const issueSummary = (tx: Transaction) => {
  if (!tx.issues.length) return { mark: '✓', cls: 'issue-ok', title: 'No issues' }
  const worst = tx.issues.some(i => i.severity === 'error') ? 'issue-error'
    : tx.issues.some(i => i.severity === 'warning') ? 'issue-warn' : 'issue-info'
  return { mark: `${worst === 'issue-error' ? '✖' : '⚠'} ${tx.issues.length}`, cls: worst,
           title: tx.issues.map(i => i.message).join('\n') }
}
```

  5. In `handleManual`, replace the `runAnalysis(fd, result => [{ ... }])` call with:

```tsx
    // The model only suggests the account; the backend splits the VAT like any other row.
    runAnalysis(fd, async result => {
      const suggested = result.transactions[0]
      const row: Transaction = {
        date: null, description, direction: type === 'revenue' ? 'in' : 'out', gross: amount.toFixed(2),
        vat: null, vat_treatment: null, net: null,
        account_code: suggested?.account_code ?? '9998',
        contra_account_code: null, currency: 'GBP', source: 'manual', method: 'user', evidence: null,
        issues: suggested
          ? suggested.issues.filter(i => i.code === 'account_not_recognised')
          : [{ code: 'account_not_recognised', severity: 'warning', message: 'No account suggested; posted to Suspense.' }],
      }
      return (await validateTransactions([row])).transactions
    })
```

  6. Replace the Step 2 table's `<thead>…</thead><tbody>…</tbody>` with:

```tsx
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Description</th>
                        <th>In / Out</th>
                        <th>Amount</th>
                        <th>VAT</th>
                        <th>Account</th>
                        <th>Check</th>
                      </tr>
                    </thead>
                    <tbody>
                      {transactions.map((tx, i) => {
                        const check = issueSummary(tx)
                        return (
                          <tr key={i}>
                            <td>{tx.date ?? '—'}</td>
                            <td>{tx.description}</td>
                            <td>
                              <span className={`badge-type ${tx.direction === 'out' ? 'badge-expense' : 'badge-revenue'}`}>
                                {tx.direction === 'out' ? 'money out' : 'money in'}
                              </span>
                            </td>
                            <td className="amount-cell">{money(tx.gross)}</td>
                            <td className="amount-cell">{money(tx.vat)}</td>
                            <td>{tx.account_code} {tx.account_name ?? ''}</td>
                            <td className={check.cls} title={check.title}>{check.mark}</td>
                          </tr>
                        )
                      })}
                    </tbody>
```

  7. Replace the trial balance table's `<thead>`, `<tbody>` and `<tfoot>` with:

```tsx
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left' }}>Account</th>
                        <th>Debit (£)</th>
                        <th>Credit (£)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tbResult.lines.map(line => (
                        <tr key={line.code}>
                          <td className="tb-account">{line.code} {line.name}</td>
                          <td className={Number(line.debit) > 0 ? 'debit-val' : 'empty-cell'}>
                            {Number(line.debit) > 0 ? money(line.debit) : '—'}
                          </td>
                          <td className={Number(line.credit) > 0 ? 'credit-val' : 'empty-cell'}>
                            {Number(line.credit) > 0 ? money(line.credit) : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="tb-total-row">
                        <td>Totals</td>
                        <td className="debit-val">{money(tbResult.total_debits)}</td>
                        <td className="credit-val">{money(tbResult.total_credits)}</td>
                      </tr>
                    </tfoot>
```

  8. In the balance pill, replace `Math.abs(tbResult.total_debits - tbResult.total_credits).toFixed(2)` with `Math.abs(Number(tbResult.total_debits) - Number(tbResult.total_credits)).toFixed(2)`.

- [ ] **Step 3: `globals.css`.** Append:

```css
.issue-ok    { color: var(--green); text-align: center; }
.issue-info  { color: var(--text-secondary); text-align: center; cursor: help; }
.issue-warn  { color: var(--amber); text-align: center; cursor: help; }
.issue-error { color: var(--red); text-align: center; cursor: help; }
```

- [ ] **Step 4: Verify.** Run `(cd frontend && npx tsc --noEmit)`, then `(cd frontend && NEXT_TELEMETRY_DISABLED=1 npm run build)`, then restore `frontend/next-env.d.ts` with git. In the browser pane (`api-slow` + `web`), paste `BT Business Broadband monthly bill 72.00`. Expected:
  - The row shows money out, £72.00, VAT £12.00, account 7502 Telephone and Internet, and a ⚠ 1 (VAT estimated) check.
  - A manual entry (Office chair, 500, expense) shows VAT £83.33 split out and a user-typed amount.
  - The trial balance lists 1200 / 2201 / 7502 and shows **✓ Trial Balance Balances**.
- [ ] **Step 5: Commit** — `git add frontend/src/lib/api.ts frontend/src/app/page.tsx frontend/src/app/globals.css && git commit -m "Frontend: show ledger transactions and the double-entry trial balance"`

---

### Task 9: Measure against the baseline

- [ ] **Step 1: Run.** Start `api-slow` only (stop the dev server). Then run:

```bash
.venv/bin/python eval/run_eval.py --label phase3 --note "Phase 3 API (double entry, adapter over the Phase 1 prompt) on an 8 GB Apple M2 with OLLAMA_TIMEOUT=300; qwen2.5vl:3b, num_ctx 4096, num_predict 2048; harness --timeout 600"
```

Run it in the background and resume with `--resume` if interrupted. Expected: the trial balance balances on every case whose rows can be posted; account and VAT accuracy are now scored; table recall is above the baseline's 0.214 because negative bank rows are no longer dropped.

- [ ] **Step 2:** Stop the API and run `ollama stop qwen2.5vl:3b`.

- [ ] **Step 3: README.** Under "Run", add the ledger endpoints:

````markdown
The ledger endpoints use double entry: `GET /api/accounts` lists the chart (Sage 50-style
codes), `POST /api/transactions/validate` splits VAT and lists issues, and
`POST /api/trial-balance` takes `{"transactions": [...], "settings": {"vat_registered": true}}`
and always balances; rows with errors (unknown account, non-GBP, impossible VAT) get a 422.
Money is sent and returned as strings, e.g. `"12.50"`.
````

- [ ] **Step 4: Commit** — `git add eval/results/2026-09-28-phase3.json README.md && git commit -m "Record Phase 3 accuracy run"`
