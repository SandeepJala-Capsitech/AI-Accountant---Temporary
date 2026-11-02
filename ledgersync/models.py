"""API and ledger models. Money is Decimal inside and a string in JSON ("12.50")."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field, computed_field, field_validator

from .accounts import BANK, BY_CODE
from .money import VatTreatment, ZERO, to_money


class Direction(str, Enum):
    IN = "in"      # money into the bank
    OUT = "out"    # money out of the bank


# The kind of document a row comes from. "agent_statement" is a letting or managing agent's statement: what
# it collected and paid for the business, and the net it paid over. "payroll" is a payroll report: the wages the
# business owes for a pay date. The last six are not transactions: a person ticks Include to book one as an
# invoice. "other" is what the adapter makes of a type it does not know.
DocumentType = Literal["receipt", "invoice", "expense_claim", "statement", "agent_statement", "payroll", "quote",
                       "pro_forma", "purchase_order", "remittance_advice", "supplier_statement", "other"]
DOCUMENT_TYPES: tuple[str, ...] = get_args(DocumentType)
NOT_TRANSACTIONS = frozenset(DOCUMENT_TYPES[6:])

# The kind of business a client is: a limited company's owners go through 2250 Director's Loan Account,
# anyone else's through 3260 Drawings and 3010 Capital Introduced.
BusinessType = Literal["limited_company", "sole_trader", "partnership", "llp"]
BUSINESS_TYPES: tuple[str, ...] = get_args(BusinessType)

# The kinds of account money moves through: each of a client's has its own code, from accounts.BANK_SLOTS.
BankAccountKind = Literal["current", "savings", "credit_card", "petty_cash"]


class BankAccount(BaseModel):
    """One of a client's bank accounts or cards: its name, its kind, and its code in the chart ("" until
    ledger.assign_codes gives it the next free one of its kind)."""
    code: str = ""
    name: str
    kind: BankAccountKind

    @field_validator("code", "name", mode="before")
    @classmethod
    def _trimmed(cls, value):
        return "" if value is None else str(value).strip()


class Issue(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"]
    related: list[int] = Field(default_factory=list)   # the other rows it is about (Show both): places in the rows
                                                       # matched; in a client's ledger, their row ids


class BusinessSettings(BaseModel):
    business_name: str = ""
    vat_registered: bool = True
    business_type: Optional[BusinessType] = None   # None: not known, as for an analysis without a client
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    books_start: Optional[dt.date] = None      # the first day of the client's first year: a row dated before it is
                                               # already in the opening balances
    bank_account: str = BANK                   # the default bank: a receipt's, and a bank line's without its own
    bank_accounts: tuple[str, ...] = (BANK,)   # the client's bank accounts and cards, by code


class Settlement(BaseModel):
    """A payment applied to a document. On a bank line: a document it pays. On a document's rows: a bank
    line that paid it. In a bank line's candidates: a document it could pay, with what is open on it."""
    ref: str
    amount: Decimal
    date: Optional[dt.date] = None
    description: str = ""
    kind: Optional[str] = None     # the kind of document or line it is (a document_type): what was paid, or what paid


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
    bank_account: Optional[str] = None             # an input on a bank line: the account or card its statement is
                                                   # for; None = the business's default bank
    currency: str = "GBP"
    source: str = "text"
    method: Literal["parsed", "llm", "vlm", "user"] = "llm"
    evidence: Optional[str] = None
    # Inputs too: what the row comes from and who it is with (from the model), the reference shared by
    # the rows of one document (from the adapter), and a person's Include tick on a document that is
    # not a transaction. Rows without a type, as older clients send them, are receipts: paid when issued.
    document_type: DocumentType = "receipt"
    counterparty: Optional[str] = None
    document_number: Optional[str] = None          # the invoice, receipt or claim number printed on it
    agent: Optional[str] = None                    # on an agent's statement: the agent, who holds the money
    owed_to: Optional[Literal["net_pay", "paye_ni", "pension"]] = None   # on a payroll report: what the row is owed to
    document_total: Optional[Decimal] = None       # the total printed on its document (an agent's statement: the
                                                   # net paid over), which matching checks its rows against
    document_net: Optional[Decimal] = None         # the total before VAT printed on its document, if it prints one
    not_vat_invoice: bool = False                  # its document says it is not a VAT invoice
    document_ref: Optional[str] = None
    include: bool = False
    # Inputs set only by the accountant chat's decisions (never by a person's edit, so not in rows.EDITABLE or
    # RowPatch): a decision can leave a row out of the books, or replace it with another row.
    excluded: bool = False
    superseded_by: Optional[str] = None            # the decision that replaced this row
    decision_ids: list[str] = Field(default_factory=list)   # the decisions that changed this row
    supplier_vat_number: Optional[str] = None      # the VAT number printed on its document, which decides whether
                                                   # the VAT on a receipt can be reclaimed
    apart_from: list[int] = Field(default_factory=list)   # rows a person said are not the same as this one, by row
                                                   # id (Not the same): never paired with it or asked about
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
    document_direction: Optional[Direction] = None  # a document's row: which way what it owes goes (in: they owe
                                                   # the business), which the rows of an agent's statement don't share
    paid_by: list[Settlement] = Field(default_factory=list)           # a document's row: what paid it
    in_opening: bool = False                       # dated before the client's first year (checks.normalise): its
                                                   # amount is in the opening balances, so booked only if included
    copy_of: Optional[Settlement] = None           # a document's row: the earlier document it repeats (a
                                                   # reminder, a receipt read twice); not booked unless included
    recorded_by: Optional[Settlement] = None       # a bank line or a claim line: the receipt or invoice for the
                                                   # same payment, booked instead; this row is not booked
    claimed_in: Optional[Settlement] = None        # a receipt or invoice on an expense claim: owed to the claimant
    vat_found: Optional[Decimal] = None            # a receipt or invoice on a claim: VAT its prices and the claim agree
                                                   # on, not booked because it says it is not a VAT invoice (Book VAT)
    date_found: Optional[dt.date] = None           # a receipt, or a claim line, whose other record (its claim line,
                                                   # receipt or card payment) agrees on all but the date: its date (Use)
    amount_found: Optional[Decimal] = None         # the same, when the two agree on the shop and the day but not the
                                                   # amount: the other record's amount (Use)
    issues: list[Issue] = Field(default_factory=list)

    @field_validator("gross", mode="before")
    @classmethod
    def _positive_pennies(cls, value):
        money = to_money(value)
        if money is None or money <= 0:
            raise ValueError("gross must be a positive amount; direction says whether money went in or out")
        return money

    @field_validator("vat", "vat_posted", "net", "owed", "balance", "opening_balance", "closing_balance",
                     "document_total", "document_net", "vat_found", "amount_found", mode="before")
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
    lines: list[TrialBalanceLine]          # the opening balances plus the movements
    opening: list[TrialBalanceLine] = Field(default_factory=list)   # the opening balances alone
    total_debits: Decimal
    total_credits: Decimal
    is_balanced: bool
    journal: list[JournalLine]
    total_income: Decimal = ZERO
    total_expenses: Decimal = ZERO
    net_profit: Decimal = ZERO
    total_assets: Decimal = ZERO
    total_liabilities: Decimal = ZERO


class SuspenseItem(BaseModel):
    """A row whose posted lines leave its gross in Suspense, while it waits for the document it needs."""
    row_id: int
    description: str
    gross: Decimal
    needs: Optional[str] = None   # "receipt_or_invoice" or "vat_invoice" while it is still held; None once it isn't


class PostedTrialBalance(TrialBalance):
    """The trial balance of the posted ledger: the opening balances plus every entry posted, and the rows posted to
    Suspense. Its journal is the posted entries, each with its row id as its transaction. problems: why the opening
    or what the Suspense items need couldn't be worked out (rows need fixing)."""
    suspense: list[SuspenseItem] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class PostResult(BaseModel):
    """What one posting wrote: its batch (None when it wrote nothing), the new lines and the reversals of lines
    posted before, their totals, and why nothing was written when the batch failed its checks."""
    batch_id: Optional[str]
    entries_added: int
    reversals_added: int
    total_debits: Decimal
    total_credits: Decimal
    problems: list[str] = Field(default_factory=list)


class ClientFields(BaseModel):
    """What a person enters for a client, trimmed; ledger.check_client says what is missing."""
    name: str
    business_type: BusinessType
    contact_name: str                  # the responsible person: the client's own contact
    first_year_end: Optional[dt.date]  # the end of the first year the client's books are kept for; sent always, and
                                       # check_client refuses it empty
    contact_email: str = ""
    contact_phone: str = ""
    vat_registered: bool = True
    bank_accounts: list[BankAccount] = Field(
        default_factory=lambda: [BankAccount(code=BANK, name="Bank current account", kind="current")])
    folder: str = ""                   # where the client's files are on the computer the site runs on; "" for none

    @field_validator("name", "contact_name", "contact_email", "contact_phone", "folder", mode="before")
    @classmethod
    def _trimmed(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("first_year_end", mode="before")
    @classmethod
    def _empty_date(cls, value):
        return None if value == "" else value


class ClosedYear(BaseModel):
    """A year the accountant closed: locked until reopened, and its balances at its end by account, signed with
    debits positive, which the next year opens with."""
    start: dt.date
    end: dt.date
    closed_at: str
    closing: dict[str, Decimal]


class Client(ClientFields):
    id: int
    closed_years: list[ClosedYear] = Field(default_factory=list)   # in order, the first year first
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
    bank_accounts: Optional[list[BankAccount]] = None
    first_year_end: Optional[dt.date] = None
    folder: Optional[str] = None
    archived: Optional[bool] = None


class FolderImport(BaseModel):
    """Which year, and for a statement which bank account, the files of a client's folder are read into."""
    year_start: dt.date
    bank_account: Optional[str] = None


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
    bank_account: Optional[str] = None           # the account or card its statement lines are on; None: the default
    year_start: Optional[dt.date] = None         # the year it was added to, which its undated rows wait in


class UploadPatch(BaseModel):
    """A change to an upload: the bank account or card its statement is for."""
    bank_account: str


class SavedRow(Transaction):
    """A saved row as the pages show it: its id, its upload, and what was read before a person's first edit."""
    id: int
    upload_id: int
    edited: bool = False
    original: Optional[dict] = None


class Year(BaseModel):
    """One of a client's accounting years."""
    start: dt.date
    end: dt.date
    label: str                 # "Year to 31 Mar 2026"
    closed: bool = False
    changed_since_closed: bool = False   # a closed year whose rows now work out to other balances than it closed with


def _money(value) -> Decimal:
    money = to_money(value if value not in (None, "") else "0")
    if money is None:
        raise ValueError("not an amount")
    return money


class OpeningLine(BaseModel):
    """A first year's opening balance on one balance sheet account, from the client's last accounts."""
    code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    @field_validator("debit", "credit", mode="before")
    @classmethod
    def _pennies(cls, value):
        return _money(value)


class OpenItem(BaseModel):
    """An invoice unpaid when the first year starts, behind Debtors (in: a customer owes the client) or Creditors
    (out: the client owes a supplier). A payment in any year can clear it."""
    id: str
    direction: Direction
    counterparty: str
    amount: Decimal
    number: Optional[str] = None
    date: Optional[dt.date] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _pennies(cls, value):
        return _money(value)

    @field_validator("id", "counterparty", mode="before")
    @classmethod
    def _trimmed(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("number", mode="before")
    @classmethod
    def _trimmed_number(cls, value):
        return None if value is None else (str(value).strip() or None)


class OpeningBalances(BaseModel):
    """A client's first year's opening balances: the amounts and the unpaid invoices behind Debtors and Creditors."""
    lines: list[OpeningLine] = Field(default_factory=list)
    unpaid: list[OpenItem] = Field(default_factory=list)


class Ledger(BaseModel):
    """One of a client's years: its rows, and the uploads that hold them or were added to it."""
    client: Client
    year: Year
    years: list[Year]
    uploads: list[Upload]
    transactions: list[SavedRow]


class InvoiceSummary(BaseModel):
    """One sales or purchase invoice of a year, with what has been paid and what is still owed after every payment
    matched to it, in any year. brought_forward: dated before the year and still owed when it started."""
    ref: str
    kind: Literal["sale", "purchase"]
    counterparty: Optional[str] = None
    number: Optional[str] = None
    date: Optional[dt.date] = None
    total: Decimal
    paid: Decimal
    owed: Decimal
    paid_by: list[Settlement] = Field(default_factory=list)
    brought_forward: bool = False


class BankCheck(BaseModel):
    """A bank account's balance in the books against the closing balance of its latest statement of the year, both
    as the statement prints it (a card's, what is owed on it). difference: books less statement."""
    code: str
    name: str
    kind: BankAccountKind
    books: Decimal
    statement: Optional[Decimal] = None
    statement_date: Optional[dt.date] = None
    difference: Optional[Decimal] = None


class YearSummary(BaseModel):
    """A year at a glance: its rows and those to review, what is unpaid, its bank checks and whether its trial
    balance can be worked out (blocked, with the problems that stop it)."""
    year: Year
    rows: int
    to_review: int
    unpaid_sales: Decimal
    unpaid_purchases: Decimal
    bank: list[BankCheck]
    trial_balance: Literal["balanced", "blocked"]
    problems: list[str] = Field(default_factory=list)


class RowPatch(BaseModel):
    """A person's change to a saved row: Link, Include or Not the same, an edit of what was read, or revert. Only
    the fields sent change; ledger.change_row checks them."""
    link: Optional[list[str]] = None
    include: Optional[bool] = None
    apart_from: Optional[list[int]] = None
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
    year_start: Optional[dt.date] = None   # the year the rows are added to, which any undated row waits in


# ─── The accountant chat's records ─────────────────────────────────────────────

class Evidence(BaseModel):
    """What a decision rests on: a quote from one of the client's uploads."""
    upload_id: int
    upload_name: str = ""
    quote: str


class DecisionDraft(BaseModel):
    """A decision as its maker states it: what was done to which rows, by which rule, on what evidence. before and
    after hold the changed fields of each row, keyed by str(row_id). AgentRecords.append_decision adds the rest."""
    client_id: int
    thread_id: Optional[str] = None
    year_start: dt.date
    action: str
    rule: str                                  # e.g. "R2 Duplicate: kept the receipt"
    status: Literal["applied", "refused"]
    row_ids: list[int] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    reason: str = ""
    before: dict[str, dict] = Field(default_factory=dict)
    after: dict[str, dict] = Field(default_factory=dict)
    actor: str                                 # "agent:<model>", "user" or "rules"
    reverses: Optional[str] = None             # the decision this one undoes


class Decision(DecisionDraft):
    """A decision as the log keeps it. Each client's decisions form a hash chain: hash is the SHA-256 of
    prev_hash (the previous decision's hash, "" for the first) and this decision's JSON without its hash."""
    id: str
    created_at: str
    prev_hash: str
    hash: str


class QuestionDraft(BaseModel):
    """A question for the person, as the rules or the agent state it; a new question is open."""
    client_id: int
    year_start: dt.date
    row_ids: list[int] = Field(default_factory=list)
    kind: Literal["date", "receipt_or_invoice", "vat_invoice", "other"]
    text: str                                  # "When was this taxi?"
    why: str                                   # "£50 or more, so I need ..."
    source: Literal["rules", "agent"]


class Question(QuestionDraft):
    """A question as kept: open until answered, waiting when the person has no answer yet ("I don't have it",
    "Not sure"), resolved once answered."""
    id: str
    status: Literal["open", "waiting", "resolved"]
    answer: Optional[str] = None
    created_at: str
    resolved_at: Optional[str] = None


class ChatThread(BaseModel):
    """One conversation with the accountant, about one client's year."""
    id: str
    client_id: int
    year_start: dt.date
    title: str
    created_at: str
    updated_at: str
