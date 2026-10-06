"""API and ledger models. Money is Decimal inside and a string in JSON ("12.50")."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field, computed_field, field_validator

from .accounts import BANK, BY_CODE
from .money import VatTreatment, to_money


class Direction(str, Enum):
    IN = "in"      # money into the bank
    OUT = "out"    # money out of the bank


# The kind of document a row comes from. The last six are not transactions: a person ticks Include to
# book one as an invoice. "other" is what the adapter makes of a type it does not know.
DocumentType = Literal["receipt", "invoice", "expense_claim", "statement", "quote", "pro_forma",
                       "purchase_order", "remittance_advice", "supplier_statement", "other"]
DOCUMENT_TYPES: tuple[str, ...] = get_args(DocumentType)
NOT_TRANSACTIONS = frozenset(DOCUMENT_TYPES[4:])

# The kind of business a client is: a limited company's owners go through 2250 Director's Loan Account,
# anyone else's through 3260 Drawings and 3000 Capital Introduced.
BusinessType = Literal["limited_company", "sole_trader", "partnership", "llp"]
BUSINESS_TYPES: tuple[str, ...] = get_args(BusinessType)


class Issue(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"]


class BusinessSettings(BaseModel):
    business_name: str = ""
    vat_registered: bool = True
    business_type: Optional[BusinessType] = None   # None: not known, as for an analysis without a client
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    bank_account: str = BANK


class Settlement(BaseModel):
    """A payment applied to a document. On a bank line: a document it pays. On a document's rows: a bank
    line that paid it. In a bank line's candidates: a document it could pay, with what is open on it."""
    ref: str
    amount: Decimal
    date: Optional[dt.date] = None
    description: str = ""


class StatementCheck(BaseModel):
    """Whether a bank statement's rows agree with the balances it prints: ok, a gap of `difference`, or none
    when it prints no balances to compare."""
    status: Literal["ok", "gap", "none"]
    difference: Optional[Decimal] = None


class Transaction(BaseModel):
    date: Optional[dt.date] = None
    description: str = ""
    direction: Direction
    gross: Decimal
    # Inputs keep what the document (or the user) said; checks.normalise never changes them,
    # so an edited row validated again is worked out afresh (new account, new settings).
    vat: Optional[Decimal] = None                  # VAT shown on the document; None = not shown
    vat_treatment: Optional[VatTreatment] = None   # rate a person picked, to estimate VAT not shown; None = none picked
    # Outputs, recomputed by checks.normalise on every pass:
    vat_posted: Optional[Decimal] = None           # the VAT the ledger books (shown, estimated or none)
    net: Optional[Decimal] = None                  # gross minus vat_posted
    account_code: str
    contra_account_code: Optional[str] = None      # None = the business's bank account
    currency: str = "GBP"
    source: str = "text"
    method: Literal["parsed", "llm", "vlm", "user"] = "llm"
    evidence: Optional[str] = None
    # Inputs too: what the row comes from and who it is with (from the model), the reference shared by
    # the rows of one document (from the adapter), and a person's Include tick on a document that is
    # not a transaction. Rows without a type, as older clients send them, are receipts: paid when issued.
    document_type: DocumentType = "receipt"
    counterparty: Optional[str] = None
    document_ref: Optional[str] = None
    include: bool = False
    link: Optional[list[str]] = None               # a person's decision on a bank line: None automatic,
                                                   # [] not a payment of any document, refs: pays these
    # Inputs too, on a bank statement row: the running balance printed on its line and the statement's
    # opening and closing balances, which statements.check_statement compares with the rows.
    balance: Optional[Decimal] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    # Outputs, recomputed by matching.match on every pass:
    paid_against: Optional[str] = None             # a bank line that pays documents: the account it settles
    pays: list[Settlement] = Field(default_factory=list)              # a bank line: the documents it pays
    candidates: list[list[Settlement]] = Field(default_factory=list)  # a bank line: what it could pay (Link)
    owed: Optional[Decimal] = None                 # a document's row: what is still open on the document
    paid_by: list[Settlement] = Field(default_factory=list)           # a document's row: what paid it
    issues: list[Issue] = Field(default_factory=list)

    @field_validator("gross", mode="before")
    @classmethod
    def _positive_pennies(cls, value):
        money = to_money(value)
        if money is None or money <= 0:
            raise ValueError("gross must be a positive amount; direction says whether money went in or out")
        return money

    @field_validator("vat", "vat_posted", "net", "owed", "balance", "opening_balance", "closing_balance", mode="before")
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

    @computed_field
    @property
    def paid_against_name(self) -> Optional[str]:
        account = BY_CODE.get(self.paid_against or "")
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
