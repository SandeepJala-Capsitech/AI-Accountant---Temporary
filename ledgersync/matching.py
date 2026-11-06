"""Matches bank lines to the open invoices and claims they pay (design of 2026-10-06), sets aside documents read
twice, and pairs card payments with their receipts. Pure functions: the same rows give the same matches, whatever
order they arrived in."""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Optional, Sequence

from .accounts import BY_CODE, CLEARING, CREDITORS, DEBTORS, STAFF_EXPENSES, AccountType
from .checks import MATCHING_ISSUES, PAYROLL_OWED, booked, issue, long_date, reclaims_vat
from .models import BusinessSettings, Direction, Issue, OpenItem, Settlement, Transaction
from .payees import processor
from .money import PENNY, ZERO, VatTreatment

WINDOW = dt.timedelta(days=31)   # a payment may be suggested for a document dated within 31 days of it
PART_WINDOW = dt.timedelta(days=120)  # a part payment, an overpayment or a set of documents: at most 120 days after
CARD_DAYS = dt.timedelta(days=3)  # a card payment reaches the bank within a few days of its receipt
TRANSFER_DAYS = dt.timedelta(days=3)  # money moved between two of the client's accounts arrives within a few days
YEAR = dt.timedelta(days=366)     # records further apart than this are different purchases, whatever else agrees
MAX_SET = 5                      # one payment clears at most five documents from one counterparty

# Words that say nothing about who a business is: legal forms and small linking words, bank-statement
# noise (payment types and their short codes), generic trade words, and place and national words.
_IGNORED = {"ltd", "limited", "plc", "llp", "co", "the", "and", "for", "of", "to", "at", "in", "on",
            "bank", "payment", "payments", "fin", "card", "dd", "so", "bacs", "fps", "ref", "direct", "debit",
            "credit", "faster", "standing", "order", "transfer", "tfr", "deb", "vis", "pos", "bgc", "chq", "atm",
            "int", "services", "solutions", "group", "online", "international", "holdings", "company", "trading",
            "uk", "gb", "london", "british", "national", "royal", "england", "scotland", "wales"}


_LEGAL = {"ltd", "limited", "plc", "llp", "co", "the"}   # what initials leave out


def _initials(name: Optional[str]) -> str:
    """NCP for National Car Parks Ltd: the first letters of a name of three words or more."""
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _LEGAL]
    return "".join(w[0] for w in words) if len(words) >= 3 else ""


def _named_in(name: Optional[str], text: str) -> bool:
    """Every word of the name is a word of the text: EE in "Matt Barnes - EE, mobile phone bill". Too loose to pair
    on, as BT is in BT Sport, but enough to ask whether two records are one."""
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _LEGAL]
    return bool(words) and set(words) <= set(re.findall(r"[a-z]+", text.lower()))


def _words(name: Optional[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _IGNORED]


def _abbreviates(short: str, long: str) -> bool:
    """'mgmt' abbreviates 'management': three letters or more, the same first letter, the rest in order."""
    if not 3 <= len(short) < len(long) or short[0] != long[0]:
        return False
    letters = iter(long)
    return all(ch in letters for ch in short)


def _within(short: list[str], long: list[str]) -> bool:
    """The whole of one name, its words in order, inside the other: "R+R PR" in "R+R PR LTD CL AC"."""
    return any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True, after legal forms, bank words, generic trade words and numbers are set aside, when two names are
    the same however short their words ("HML PM Ltd"), when the whole of one, three letters or more, is in the
    other, when they share a word of four or more letters, when a word of one abbreviates a word of the other, or
    when a word of one is the other's initials (NCP, National Car Parks)."""
    wa, wb = _words(a), _words(b)
    if any(w == initials for w, initials in ((w, _initials(b)) for w in wa) if len(w) >= 3) or any(
            w == initials for w, initials in ((w, _initials(a)) for w in wb) if len(w) >= 3):
        return True
    if wa and wa == wb:
        return True
    short, long = sorted((wa, wb), key=len)
    if short and sum(map(len, short)) >= 3 and _within(short, long):
        return True
    return (any(w in wb for w in wa if len(w) >= 4)
            or any(_abbreviates(x, y) or _abbreviates(y, x) for x in wa for y in wb))


# What a bank line's reference can add to a payee's name about why it was paid: never part of who was paid.
_REFERENCE = {"expenses", "expense", "claim", "claims", "reimbursement", "refund"}
# Trade words many businesses share: part of a name, but two names sharing only these may be anyone.
_GENERIC = {"print", "printing", "printers", "consulting", "consultancy", "consultants", "design", "designs",
            "media", "marketing", "management", "associates", "partners", "enterprises", "systems", "supplies",
            "office", "studio", "digital", "technology", "technologies", "cabs", "taxis", "cars", "hotel", "hotels",
            "cafe", "restaurant", "shop", "store", "stores", "city", "solicitors", "accountants", "builders"}


def _same_word(x: str, y: str) -> bool:
    """One word, as two names write it: the same, or one abbreviates the other ('mgmt', 'management')."""
    return x == y or _abbreviates(x, y) or _abbreviates(y, x)


def _inside(short: list[str], long: list[str]) -> bool:
    """Every word of the shorter name is a word of the longer, or abbreviates one, or is the initial of one ('M' of
    'Matt'), and at least one word that isn't a trade word many share is found whole or abbreviated."""
    distinctive = False
    for word in short:
        found = [w for w in long if _same_word(word, w)]
        if found:
            distinctive = distinctive or word not in _GENERIC
        elif not (len(word) == 1 and any(w[0] == word for w in long)):
            return False
    return distinctive


def same_payee(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two names are surely the same payee, strictly enough to pay a document automatically. After legal
    forms, bank words, place and national words and the payment's reference are set aside: the names are the
    same ("HML PM Ltd"); or every word of the shorter is in the longer, whole, abbreviated or as an initial, with at
    least one that isn't a trade word many businesses share ("BRIGHTLINE PRI" and "Brightline Print Ltd", "M
    BARNES" and "Matt Barnes", but not "Brightline Print" and "Acme Print Ltd", nor "Leeds Print" and "Leeds City
    Cabs"); or one name is the other's initials (NCP, National Car Parks). names_match, which one shared word
    satisfies, only suggests a document for a person to Link."""
    wa, wb = ([w for w in _words(name) if w not in _REFERENCE] for name in (a, b))
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    if any(len(i) >= 3 and words == [i] for words, i in ((wa, _initials(b)), (wb, _initials(a)))):
        return True
    short, long = sorted((wa, wb), key=len)
    if sum(map(len, short)) < 3:   # "BT" in "BT Sport": too short to tell
        return False
    return _inside(short, long) or (len(short) == len(long) and _inside(long, short))


@dataclass
class _Document:
    """An open invoice, claim or included document: the rows that share its document_ref."""
    ref: str
    kind: str                     # its document type
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
        return Settlement(ref=self.ref, amount=amount, date=self.date, description=self.description, kind=self.kind)

    def label(self) -> str:
        when = f", {self.date:%d %b %Y}" if self.date else ""
        return f"{self.description} ({_gbp(self.open)}{when})"


@dataclass
class DocumentStatus:
    """What is still open on a document after every payment matched to it, and the payments."""
    open: Decimal
    paid_by: list[Settlement]


def _brought_forward(items: Sequence[OpenItem]) -> dict[str, _Document]:
    """The invoices unpaid when the first year starts, as open documents with no rows: a customer's on Debtors, a
    supplier's on Creditors, each dated by its item (books dates them the day before the first year starts)."""
    return {f"ob-{item.id}": _Document(ref=f"ob-{item.id}", kind="invoice", direction=item.direction.value,
                                       account=DEBTORS if item.direction == Direction.IN else CREDITORS,
                                       counterparty=item.counterparty,
                                       description=f"Brought forward: {item.number or item.counterparty}",
                                       total=item.amount, date=item.date, open=item.amount)
            for item in items}


def _only_each_other(docs: list[_Document], lines: list[int], agree) -> list[tuple[_Document, int]]:
    """The documents and lines that agree with each other and with nothing else in the two lists."""
    found = [(d, n) for d in docs for n in lines if agree(d, n)]
    by_doc, by_line = Counter(d.ref for d, _ in found), Counter(n for _, n in found)
    return [(d, n) for d, n in found if by_doc[d.ref] == 1 and by_line[n] == 1]


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}"


def match(rows: list[Transaction], settings: BusinessSettings, read: Optional[list[Optional[Decimal]]] = None,
          apart: Optional[list[set[int]]] = None, open_items: Sequence[OpenItem] = ()) -> list[Transaction]:
    """The rows as match_documents matches them."""
    return match_documents(rows, settings, read, apart, open_items)[0]


def match_documents(rows: list[Transaction], settings: BusinessSettings,
                    read: Optional[list[Optional[Decimal]]] = None, apart: Optional[list[set[int]]] = None,
                    open_items: Sequence[OpenItem] = (), left_out: Optional[dict[str, dict[str, Decimal]]] = None
                    ) -> tuple[list[Transaction], dict[str, DocumentStatus]]:
    """Which documents each bank line pays and what is still owed on each document, for transactions
    checks.normalise has already prepared. Lines a person linked go first; then lines that pay exactly what
    one document owes, whatever their date, so a bigger payment to the same person is never taken as an
    overpayment of a document another line pays exactly; then the rest, oldest first. So the result does not
    depend on the order the rows arrived in. With a client's saved rows come what a person decided: read, each row's
    amount as it was read before a person changed it (None if unchanged), and apart, for each row, the rows a person
    said are not the same as it, which are never paired with it or asked about. open_items: the invoices unpaid when
    the client's first year started, which a bank line pays as it pays an invoice uploaded. Also each document's
    status, by its ref, brought-forward ones ("ob-{id}") among them. A row a decision left out of the books (excluded,
    or replaced by a split) is matched as if it were not there; left_out, for each document, is what its excluded
    rows amounted to in each direction, so the total printed on the document is not said to be short of them."""
    read = read or [None] * len(rows)
    apart = apart or [set() for _ in rows]
    if any(tx.excluded or tx.superseded_by for tx in rows):
        return _without_left_out(rows, settings, read, apart, open_items)
    rows = _checked_totals(_with_claims(_without_copies([_cleared(tx) for tx in rows]), settings, apart), read,
                           left_out or {})
    documents = {**_brought_forward(open_items), **_documents(rows)}
    changes: list[dict] = [{} for _ in rows]
    # Both sides of a transfer between the client's own accounts are the transfer's, never a payment of a document.
    transfers = _pair_transfers(rows, settings, changes, apart)
    lines = sorted((n for n, tx in enumerate(rows) if tx.document_type in _PAYMENT_LINES and n not in transfers
                    and booked_line(tx)),
                   key=lambda n: (rows[n].link is None, rows[n].date or dt.date.max, n))
    automatic: dict[int, list[Issue]] = {}   # the lines left to match automatically, with their issues so far
    for n in lines:
        left = _apply_link(n, rows[n], documents, changes[n])
        if left is not None:
            automatic[n] = left
    paid_exactly = True
    while paid_exactly:      # paying one document can leave another line a single exact match
        paid_exactly = False
        for n in list(automatic):
            exact = _exact(rows[n], [d for d in documents.values() if _could_pay(rows[n], d)])
            if len(exact) == 1:
                _pay(n, rows[n], exact, changes[n], automatic[n])
                _note(changes[n], rows[n], automatic.pop(n))
                paid_exactly = True
    # A card payment that a receipt records is that receipt's, not a part payment, a set, an overpayment or a payment
    # to an open account: pair those lines first. Lines that exact amounts or a person's choice decide stay as they are.
    card_lines = [n for n in automatic if rows[n].document_type == "statement"
                  and _outcome(rows[n], documents)[0] in ("pay", "none", "open_document")]
    _pair_receipts(rows, changes, card_lines, apart, first_pass_only=True)
    for n in [n for n in automatic if changes[n].get("recorded_by")]:
        _note(changes[n], rows[n], automatic.pop(n))
    for n, found in automatic.items():
        _settle(n, rows[n], documents, changes[n], found)
    statement_lines = [n for n in lines if rows[n].document_type == "statement"]
    _pair_receipts(rows, changes, statement_lines, apart)
    _paid_again(rows, changes, statement_lines, apart)
    for doc in documents.values():
        for n in doc.rows:
            changes[n].update(owed=doc.open, document_direction=Direction(doc.direction), paid_by=list(doc.paid_by))
    matched = [tx.model_copy(update=change) if change else tx for tx, change in zip(rows, changes)]
    matched = [_with_payout_report(_with_sale_without_vat(tx, settings)) for tx in matched]
    statuses = {ref: DocumentStatus(open=doc.open, paid_by=list(doc.paid_by)) for ref, doc in documents.items()}
    return _without_payroll(matched), statuses


def _without_left_out(rows: list[Transaction], settings: BusinessSettings, read: list[Optional[Decimal]],
                      apart: list[set[int]], open_items: Sequence[OpenItem]) -> tuple[list[Transaction],
                                                                                    dict[str, DocumentStatus]]:
    """match_documents on the rows a decision didn't leave out, put back in place among the left-out ones, which
    keep no outputs of a match. The places that matching's issues name are mapped back to places in all the rows."""
    kept = [n for n, tx in enumerate(rows) if not (tx.excluded or tx.superseded_by)]
    place = {n: k for k, n in enumerate(kept)}
    # Only excluded rows: a split's parts keep the document_ref of the row they replace and add up to it, so the
    # replaced row's amount is already among the kept ones.
    left_out: dict[str, dict[str, Decimal]] = {}
    for tx in rows:
        if tx.excluded and not tx.superseded_by:
            by_direction = left_out.setdefault(tx.document_ref, {})
            by_direction[tx.direction.value] = by_direction.get(tx.direction.value, ZERO) + tx.gross
    matched, statuses = match_documents([rows[n] for n in kept], settings, [read[n] for n in kept],
                                        [{place[m] for m in apart[n] if m in place} for n in kept], open_items,
                                        left_out)
    result = [_cleared(tx) for tx in rows]
    for n, tx in zip(kept, matched):
        result[n] = tx.model_copy(update={"issues": [
            i.model_copy(update={"related": [kept[p] for p in i.related]}) if i.code in MATCHING_ISSUES else i
            for i in tx.issues]})
    return result, statuses


def _with_sale_without_vat(tx: Transaction, settings: BusinessSettings) -> Transaction:
    """A VAT-registered client's bank line left unpaired, booked to an income account usually charged VAT, with no VAT
    shown and no rate a person chose: the sale may need its invoice, or a rate."""
    account = BY_CODE.get(tx.account_code)
    if (settings.vat_registered and tx.document_type == "statement" and tx.direction == Direction.IN
            and account is not None and account.type == AccountType.INCOME and account.vat == VatTreatment.STANDARD
            and tx.vat is None and tx.vat_treatment is None and not tx.pays and tx.recorded_by is None
            and booked(tx) and booked_line(tx)):
        return tx.model_copy(update={"issues": tx.issues + [issue(
            "sale_without_vat", "A sale with no VAT shown: book it from its invoice, or choose its VAT rate.")]})
    return tx


def _with_payout_report(tx: Transaction) -> Transaction:
    """A payment processor's payout held in Payment Processor Clearing, which no payout report (read as an agent's
    statement) pays yet: the report is needed to book the sales, fees and refunds it is made of."""
    if (tx.document_type == "statement" and tx.direction == Direction.IN and tx.account_code == CLEARING
            and not tx.pays and booked(tx) and booked_line(tx)):
        who = processor(tx.counterparty, tx.description) or "payment processor"
        when = f" for {long_date(tx.date)}" if tx.date else ""
        return tx.model_copy(update={"issues": tx.issues + [issue(
            "payout_report", f"A {who} payout is sales less {who}'s fees and refunds: upload the {who} payout report"
                             f"{when}, so they are booked. Held in Payment Processor Clearing until then.")]})
    return tx


def booked_line(tx: Transaction) -> bool:
    """A bank line or an agent's item that moves money in the books: not one dated before the client's first year,
    which the opening balances already hold, unless a person ticked Include."""
    return tx.include or not tx.in_opening


def _pair_transfers(rows: list[Transaction], settings: BusinessSettings, changes: list[dict],
                    apart: list[set[int]]) -> set[int]:
    """The transfers between the client's own accounts with both statements in: a line out of one of its accounts
    coded to another, and a line into that other account coded back to the first, for the same amount and dated within
    a few days, the nearest dates first. The line out books the transfer; the line in is recorded by it and not
    booked, so the money moves once. A line a person linked to documents, or said is not the same as the other, stays
    apart. The places of the lines paired."""
    own = set(settings.bank_accounts)

    def bank(tx: Transaction) -> str:
        return tx.bank_account or settings.bank_account

    lines = [n for n, tx in enumerate(rows) if tx.document_type == "statement" and not tx.link and tx.date
             and booked_line(tx) and tx.account_code in own and bank(tx) in own and tx.account_code != bank(tx)]
    pairs = sorted((abs(rows[a].date - rows[b].date), rows[a].date, a, b) for a in lines for b in lines
                   if rows[a].direction == Direction.OUT and rows[b].direction == Direction.IN
                   and rows[a].account_code == bank(rows[b]) and rows[b].account_code == bank(rows[a])
                   and rows[a].gross == rows[b].gross and abs(rows[a].date - rows[b].date) <= TRANSFER_DAYS
                   and b not in apart[a])
    paired: set[int] = set()
    for _, _, a, b in pairs:
        if a in paired or b in paired:
            continue
        paired |= {a, b}
        out = rows[a]
        changes[b]["recorded_by"] = Settlement(ref=out.document_ref or f"row-{a}", amount=out.gross, date=out.date,
                                               description=out.description, kind=out.document_type)
        changes[b]["issues"] = rows[b].issues + [issue(
            "transfer", f"The other side of {out.description} on {long_date(out.date)}: booked once, from that line.",
            "info", related=[a])]
    return paired


def _without_payroll(rows: list[Transaction]) -> list[Transaction]:
    """A payment on Net Wages, PAYE and NI or Pension Fund whose wages no payroll report books: the report must be
    dated in the payment's month or the one before."""
    months = {(tx.date.year, tx.date.month) for tx in rows if tx.document_type == "payroll" and tx.date}
    result = []
    for tx in rows:
        if tx.document_type == "statement" and tx.date and tx.account_code in PAYROLL_OWED:
            before = (tx.date.year, tx.date.month - 1) if tx.date.month > 1 else (tx.date.year - 1, 12)
            if not months & {(tx.date.year, tx.date.month), before}:
                found = issue("no_payroll", f"No payroll report for {tx.date:%B %Y}: add it so wages are booked in "
                                            "full.")
                tx = tx.model_copy(update={"issues": tx.issues + [found]})
        result.append(tx)
    return result


def _cleared(tx: Transaction) -> Transaction:
    """The row without the outputs of an earlier match."""
    return tx.model_copy(update={"paid_against": None, "pays": [], "candidates": [], "owed": None, "paid_by": [],
                                 "copy_of": None, "recorded_by": None, "claimed_in": None, "vat_found": None,
                                 "date_found": None, "amount_found": None, "documented_by": None,
                                 "issues": [i for i in tx.issues if i.code not in MATCHING_ISSUES]})


def _pair_receipts(rows: list[Transaction], changes: list[dict], lines: list[int], apart: list[set[int]],
                   first_pass_only: bool = False) -> None:
    """Each card payment that a receipt records: a bank line nothing else was matched to, for the receipt's total,
    the same way and dated within a few days of it, the nearest receipt first and one from its shop before any
    other. The receipt is booked and the line is not, so the payment counts once and its VAT is the receipt's. A
    person's Link or Unlink on the line keeps them apart, as does saying they are not the same; one receipt records
    one line. With first_pass_only, only the pairs found by amount, date and name (or amount and date alone) are
    made; a receipt already paired is never paired again."""

    def kept_apart(n: int, receipt: _Document) -> bool:
        return any(r in apart[n] for r in receipt.rows)

    receipts = list(_grouped(rows, lambda tx: tx.document_type == "receipt" and booked(tx)
                             and tx.claimed_in is None and tx.date_found is None).values())
    receipts = [r for r in receipts if not any(changes[k].get("paid_by") or changes[k].get("recorded_by")
                                               for k in r.rows)]
    for by_name in (True, False):   # names first; then the amount and the date alone, which a bank's name can't spoil
        for n in lines:   # oldest first
            line, change = rows[n], changes[n]
            if (line.link is not None or line.date is None or change.get("pays") or change.get("candidates")
                    or change.get("recorded_by")):
                continue
            fits = [r for r in receipts if r.date is not None and abs(line.date - r.date) <= CARD_DAYS
                    and r.total == line.gross and r.direction == line.direction.value
                    and (not by_name or names_match(line.counterparty, r.counterparty)) and not kept_apart(n, r)]
            if not fits:
                continue
            receipt = min(fits, key=lambda r: (abs(line.date - r.date), r.date, r.ref))
            receipts.remove(receipt)
            _record(rows, changes, n, receipt)
    if first_pass_only:
        return
    # What is left: a receipt and a bank line that agree on all but the date, and only with each other. A receipt
    # without a date takes the bank's; one with another date asks, the bank's own date being taken as right.
    open_lines = [n for n in lines if rows[n].link is None and rows[n].date is not None
                  and not (changes[n].get("pays") or changes[n].get("candidates") or changes[n].get("recorded_by"))]

    def agrees(receipt: _Document, n: int) -> bool:
        line = rows[n]
        return (receipt.total == line.gross and receipt.direction == line.direction.value
                and same_payee(line.counterparty, receipt.counterparty) and not kept_apart(n, receipt)
                and (receipt.date is None or abs(line.date - receipt.date) <= YEAR))

    settled = _only_each_other(receipts, open_lines, agrees)
    for receipt, n in settled:
        line, first = rows[n], receipt.rows[0]
        if receipt.date is None:
            _record(rows, changes, n, receipt)
            for r in receipt.rows:
                note = issue("date_from_bank", f"No date on the receipt; dated {long_date(line.date)} from the bank.",
                             "info")
                changes[r].update(date=line.date, issues=[i for i in rows[r].issues if i.code != "date_missing"]
                                  + ([note] if r == first else []))
        else:   # the same amount: one payment, booked once; the question is only its date
            _record(rows, changes, n, receipt)
            asks = issue("date_conflict", f"Read as {long_date(receipt.date)}; the bank paid on {long_date(line.date)}.",
                         related=[n])
            changes[first].update(date_found=line.date, issues=rows[first].issues + [asks])
    # Then a receipt and a bank line from the same shop within a few days, left apart because their amounts differ,
    # and only with each other: the receipt was misread, or the payment is another. The receipt asks; the bank's
    # amount is taken as right.

    def differs(receipt: _Document, n: int) -> bool:
        line = rows[n]
        return (len(receipt.rows) == 1 and receipt.date is not None and abs(line.date - receipt.date) <= CARD_DAYS
                and receipt.direction == line.direction.value and names_match(line.counterparty, receipt.counterparty)
                and not kept_apart(n, receipt))

    used, used_lines = {r.ref for r, _ in settled}, {n for _, n in settled}
    for receipt, n in _only_each_other([r for r in receipts if r.ref not in used],
                                       [n for n in open_lines if n not in used_lines], differs):
        line, first = rows[n], receipt.rows[0]
        rate = _subtotal_only(rows[first], receipt, line)
        if rate:   # the receipt was read at its total before VAT: the bank line books the payment, documented by it
            held = Settlement(ref=line.document_ref or f"row-{n}", amount=line.gross, date=line.date,
                              description=line.description, kind=line.document_type)
            for r in receipt.rows:
                changes[r]["recorded_by"] = held
            changes[n]["documented_by"] = receipt.settlement(receipt.total)
            note = issue("subtotal_only", f"The receipt prints {_gbp(receipt.total)} before VAT and no total; the bank "
                                          f"paid {_gbp(line.gross)}, which is {_gbp(receipt.total)} plus VAT at {rate}. "
                                          "Booked from the bank line with no VAT: get a complete copy to reclaim the "
                                          f"{_gbp(line.gross - receipt.total)} VAT.", related=[n])
            changes[first].update(issues=rows[first].issues + [note])
            continue
        asks = issue("amount_conflict", f"Read as {_gbp(receipt.total)}; the bank paid {_gbp(line.gross)}. Not booked "
                                        "until you choose.", related=[n])
        held = Settlement(ref=line.document_ref or f"row-{n}", amount=line.gross, date=line.date,
                          description=line.description, kind=line.document_type)
        for r in receipt.rows:   # the payment counts once meanwhile: the bank line's
            changes[r]["recorded_by"] = held
        changes[first].update(amount_found=line.gross, issues=rows[first].issues + [asks])


_RATES_IN_WORDS = ((Decimal("0.20"), "20%"), (Decimal("0.05"), "5%"))


def _subtotal_only(tx: Transaction, receipt: _Document, line: Transaction) -> Optional[str]:
    """The VAT rate, in words, when a receipt was read at its total before VAT: what it books is the net it prints, and
    the bank paid exactly that plus VAT at 20% or 5% (a receipt cut off before its VAT and total). None otherwise."""
    if tx.document_net is None or tx.document_net != receipt.total:
        return None
    return next((words for rate, words in _RATES_IN_WORDS
                 if abs(receipt.total + (receipt.total * rate).quantize(PENNY) - line.gross) <= PENNY), None)


def _paid_again(rows: list[Transaction], changes: list[dict], lines: list[int], apart: list[set[int]]) -> None:
    """Each bank line nothing was matched to whose amount, counterparty and date are those of a receipt an expense
    claim line records: the receipt books the purchase, owed to the claimant, and the line books it again. The line
    warns booked_twice, pointing at the receipt and the claim line: if the line paid it, the claim line is the one to
    drop, and the line then pays the receipt. A person's Link or Unlink on the line, or saying they are not the same,
    keeps them apart."""
    receipts = _grouped(rows, lambda tx: tx.document_type == "receipt" and tx.claimed_in is not None and booked(tx))
    claim_lines: dict[str, list[int]] = {}
    for n, tx in enumerate(rows):
        if tx.document_type == "expense_claim" and tx.recorded_by is not None:
            claim_lines.setdefault(tx.recorded_by.ref, []).append(n)
    for n in lines:
        line, change = rows[n], changes[n]
        if line.link is not None or line.date is None or change.get("pays") or change.get("recorded_by"):
            continue
        receipt = next((r for r in receipts.values() if r.total == line.gross and r.date == line.date
                        and r.direction == line.direction.value and names_match(line.counterparty, r.counterparty)
                        and not any(k in apart[n] for k in r.rows)), None)
        if receipt is not None:
            twice = issue("booked_twice", f"Also booked from the expenses sheet's receipt ({receipt.counterparty}, "
                                          f"{_gbp(receipt.total)}, {long_date(receipt.date)}). If this line paid "
                                          "it, drop the expenses sheet line: this line then pays the receipt.",
                          related=receipt.rows + claim_lines.get(receipt.ref, []))
            change["issues"] = change.get("issues", line.issues) + [twice]


def _record(rows: list[Transaction], changes: list[dict], n: int, receipt: _Document) -> None:
    """Bank line n is the card payment the receipt records: the receipt is booked, paid by it, and the line is not.
    The receipt is paid from the line's account or card, which may not be the default bank."""
    line = rows[n]
    changes[n]["recorded_by"] = receipt.settlement(receipt.total)
    if not line.include:   # it posts nothing, so its account has no effect on the books: nothing to ask
        changes[n]["issues"] = [i for i in changes[n].get("issues", line.issues) if i.code not in ("account_not_recognised", "account_unclear")]
    for r in receipt.rows:
        changes[r]["paid_by"] = [Settlement(ref=line.document_ref or f"row-{n}", amount=line.gross,
                                            date=line.date, description=line.description, kind=line.document_type)]
        changes[r]["contra_account_code"] = line.contra_account_code


# Rows that move money and so can pay a document: a bank line, and each item of an agent's statement (the rent it
# collected can pay a sales invoice, a repair it paid the plumber's bill).
_PAYMENT_LINES = frozenset({"statement", "agent_statement"})
# Notes on the VAT a row books: a claim line its receipt books books none, so they say nothing there.
_VAT_NOTES = frozenset({"vat_rate_mismatch", "vat_blocked"})


def _documents(rows: list[Transaction]) -> dict[str, _Document]:
    """The open documents: booked invoices, claims and included documents. A claim still asks for the lines its
    receipts book; an invoice on a claim is owed to the claimant, on the claim, not to its supplier. A payroll
    report is not one: what it owes is on 2220, 2210 and 2230, which the bank payments clear."""
    return _grouped(rows, lambda tx: tx.document_type not in ("receipt", "statement", "payroll")
                    and tx.claimed_in is None
                    and (booked(tx) or (tx.document_type == "expense_claim" and tx.recorded_by is not None)))


def _with_claims(rows: list[Transaction], settings: BusinessSettings, apart: list[set[int]]) -> list[Transaction]:
    """The rows, each expense claim line that a receipt or invoice also records marked as recorded by it: the same
    amount, the same way and dated within a few days, the nearest document first and one the line names before any
    other. The document is booked, since it shows the VAT that can be reclaimed, and is owed to the
    claimant on the claim's account; the claim line is not booked unless a person ticks Include, which books it as
    well and keeps it paired, so the tick can be undone."""
    documents = _grouped(rows, lambda tx: tx.document_type in ("receipt", "invoice") and booked(tx)).values()
    lines = [n for n, tx in enumerate(rows) if tx.document_type == "expense_claim" and tx.date]

    def named(line: Transaction, d: _Document) -> bool:
        return names_match(d.counterparty, _shop(line))

    def agrees(line: Transaction, d: _Document) -> bool:
        """All but the date: the amount, the way, and the line naming the document's counterparty."""
        return d.total == line.gross and d.direction == line.direction.value and named(line, d)

    def near(line: Transaction, d: _Document) -> bool:
        """The amount, the way, and dated within a few days."""
        return (d.date is not None and abs(d.date - line.date) <= CARD_DAYS and d.total == line.gross
                and d.direction == line.direction.value)

    def kept_apart(n: int, d: _Document) -> bool:
        return any(r in apart[n] for r in d.rows)

    # The claims whose lines add up to the total printed on them: that total confirms each line's amount.
    confirmed = {ref for ref, claim in _grouped(rows, lambda tx: tx.document_type == "expense_claim").items()
                 if (printed := next((rows[n].document_total for n in claim.rows if rows[n].document_total is not None),
                                     None)) is not None and claim.total == abs(printed)}
    marked, paired_lines, paired_documents = list(rows), set(), set()

    def pair(n: int, document: _Document, dated: Optional[dt.date] = None) -> None:
        """Claim line n is recorded by the document, which is owed to the claimant; dated: the document had no date,
        and takes the line's."""
        paired_lines.add(n)
        paired_documents.add(document.ref)
        line = rows[n]
        claim = Settlement(ref=line.document_ref or f"row-{n}", amount=line.gross, date=line.date,
                           kind="expense_claim", description=_claim_name(line))
        twice = []
        if line.include:   # a person ticked it: booked as well as the document, so it counts twice
            twice = [issue("booked_twice", f"Also booked from its {_NUMBERED.get(document.kind, 'document')} "
                                           f"({document.counterparty}, {_gbp(document.total)}, "
                                           f"{long_date(document.date or line.date)}). Untick unless they're separate "
                                           "purchases.", related=document.rows)]
        own = line.issues if line.include else [i for i in line.issues if i.code not in _VAT_NOTES]
        marked[n] = line.model_copy(update={"recorded_by": document.settlement(document.total), "issues": own + twice})
        found, vat = _claim_vat(line, document, rows, settings)
        if dated:
            kind = _NUMBERED.get(rows[document.rows[0]].document_type, "document")
            found = found + [issue("date_from_claim", f"No date on the {kind}; dated {long_date(dated)} from the "
                                                      "claim.", "info")]
        for k, r in enumerate(document.rows):
            issues = [i for i in rows[r].issues if not dated or i.code != "date_missing"] + (found if k == 0 else [])
            marked[r] = rows[r].model_copy(update={"claimed_in": claim, "contra_account_code": STAFF_EXPENSES,
                                                   "issues": issues, **(vat if k == 0 else {}),
                                                   **({"date": dated} if dated else {})})

    # Those whose names agree first, so two records of the same amount and day pair by name; then the rest on the
    # amount and the date alone: names can differ (a mileage log names no one), but the amount and the date don't.
    for by_name in (True, False):
        pairs = sorted(((abs(d.date - rows[n].date), rows[n].date, n, d.ref, d) for n in lines for d in documents
                        if near(rows[n], d) and (not by_name or named(rows[n], d)) and not kept_apart(n, d)),
                       key=lambda pair: pair[:4])   # the nearest dates first
        for _, _, n, ref, document in pairs:
            if n not in paired_lines and ref not in paired_documents:
                pair(n, document)
    # What is left: a document and a claim line that agree on all but the date, and only with each other. A document
    # without a date takes the line's; with another date, one of them was misread or mistyped, so both ask, and they
    # stay apart until a person says which date is right.
    open_documents = [d for d in documents if d.ref not in paired_documents]
    open_lines = [n for n in lines if n not in paired_lines]
    settled = _only_each_other(open_documents, open_lines, lambda d, n: agrees(rows[n], d) and not kept_apart(n, d)
                               and (d.date is None or abs(d.date - rows[n].date) <= YEAR))
    def ask(code: str, field: str, n: int, document: _Document, from_line, from_document,
            to_document: str, to_line: str) -> None:
        """The document and claim line n both ask which is right: each holds the other's value (field, for the
        choice), and its question points at the other (Show both)."""
        first = document.rows[0]
        marked[first] = marked[first].model_copy(update={
            field: from_line, "issues": marked[first].issues + [issue(code, to_document, related=[n])]})
        marked[n] = marked[n].model_copy(update={
            field: from_document, "issues": marked[n].issues + [issue(code, to_line, related=document.rows)]})

    for document, n in settled:
        line = rows[n]
        if document.date is None:
            pair(n, document, dated=line.date)
            continue
        pair(n, document)   # the same amount: one purchase, booked once; the question is only its date
        kind = _NUMBERED.get(document.kind, "document")
        ask("date_conflict", "date_found", n, document, line.date, document.date,
            f"Read as {long_date(document.date)}; the claim says {long_date(line.date)}.",
            f"Its {kind} was read as {long_date(document.date)}; this line says {long_date(line.date)}.")
    # Then a document on one account and a claim line from the same shop within a few days, left apart because their
    # amounts differ, and only with each other: one amount was misread, or only part of a bill is claimed.

    def differs(d: _Document, n: int) -> bool:
        line = rows[n]
        return (len(d.rows) == 1 and d.date is not None and abs(d.date - line.date) <= CARD_DAYS
                and d.direction == line.direction.value and not kept_apart(n, d)
                and (named(line, d) or _named_in(d.counterparty, _shop(line))))

    used, used_lines = {d.ref for d, _ in settled}, {n for _, n in settled}
    for document, n in _only_each_other([d for d in open_documents if d.ref not in used],
                                        [n for n in open_lines if n not in used_lines], differs):
        line = rows[n]
        kind = _NUMBERED.get(document.kind, "document")
        # The purchase counts once meanwhile: the claim line, which is what is owed, is booked and the document waits.
        held = Settlement(ref=line.document_ref or f"row-{n}", amount=line.gross, date=line.date,
                          description=line.description, kind="expense_claim")
        for r in document.rows:
            marked[r] = marked[r].model_copy(update={"recorded_by": held})
        total = (f" The claim's total confirms {_gbp(line.gross)}; {_gbp(document.total)} puts it "
                 f"{_gbp(abs(document.total - line.gross))} out." if line.document_ref in confirmed else "")
        ask("amount_conflict", "amount_found", n, document, line.gross, document.total,
            f"Read as {_gbp(document.total)}; the claim says {_gbp(line.gross)}. Not booked until you choose.",
            f"Its {kind} was read as {_gbp(document.total)}; this line says {_gbp(line.gross)}.{total}")
    return marked


def _shop(line: Transaction) -> str:
    """Where a claim line was spent: its description without the claimant's name, which is on every line of the claim
    (Barnes Plumbing is not where Matt Barnes spent)."""
    claimant = set(re.findall(r"[a-z]+", (line.counterparty or "").lower()))
    return " ".join(w for w in re.findall(r"[A-Za-z0-9&+]+", line.description) if w.lower() not in claimant)


def _claim_name(line: Transaction) -> str:
    return f"{line.counterparty}'s expense claim" if line.counterparty else "the expense claim"


def _claim_vat(line: Transaction, document: _Document, rows: list[Transaction],
               settings: BusinessSettings) -> tuple[list[Issue], dict]:
    """The claim line's VAT that its receipt or invoice does not show, with the change to the document's (first) row.
    Only what the document shows can be reclaimed, so it is a warning; but when the document, on one account,
    prints its total before VAT and that is its total less the claim's VAT, its own prices show the VAT: it is
    booked, or, when the document says it is not a VAT invoice, offered (Book VAT) for once the VAT invoice is in."""
    shown = sum((rows[r].vat or ZERO for r in document.rows), ZERO)
    if not line.vat or shown >= line.vat:
        return [], {}
    first = rows[document.rows[0]]
    kind = _NUMBERED.get(first.document_type, "document")
    if (len(document.rows) == 1 and first.vat is None and first.document_net is not None
            and document.total - first.document_net == line.vat and reclaims_vat(first, settings)):
        prices = f"{_gbp(first.document_net)} before VAT, {_gbp(document.total)} in all"
        if first.not_vat_invoice:
            offer = issue("not_vat_invoice", f"VAT {_gbp(line.vat)} ({prices}) can't be reclaimed without a VAT "
                                             "invoice. Get it, then Book VAT.")
            return [offer], {"vat_found": line.vat}
        return ([issue("vat_worked_out", f"VAT {_gbp(line.vat)} booked from its prices: {prices}.", "info")],
                {"vat_posted": line.vat, "net": first.gross - line.vat})
    on_it = f"only {_gbp(shown)}" if shown else "none"
    return [issue("claim_vat", f"The claim shows VAT {_gbp(line.vat)}; the {kind} shows {on_it}. Only the {kind}'s "
                               "VAT is reclaimed.")], {}


# How a message names each kind of document, beyond the numbered ones.
_KIND_WORDS = {"agent_statement": "agent's statement", "quote": "quote", "pro_forma": "pro forma invoice",
               "purchase_order": "purchase order", "remittance_advice": "remittance advice",
               "supplier_statement": "supplier's statement"}


def _checked_totals(rows: list[Transaction], read: list[Optional[Decimal]],
                    left_out: dict[str, dict[str, Decimal]]) -> list[Transaction]:
    """The rows, each document that doesn't add up to the total printed on it saying so once, with what is missing or
    too much, and pointing at its other rows (Show its lines). It says so on the row that explains it when one does:
    a row a person changed by just that much, or a row of just that much read twice, or the one row of that much;
    otherwise on the first row. Worked out afresh every time, so the warning goes once a line the reading missed is
    added, or a row read twice is removed. left_out is, for each document, the total of its rows a decision left out of
    the books: a document that adds up with them is not short of anything, only a decision has set them aside."""
    marked = list(rows)
    for doc in _grouped(rows, lambda tx: tx.document_type != "statement").values():
        printed = next((rows[n].document_total for n in doc.rows if rows[n].document_total is not None), None)
        claimed_in = rows[doc.rows[0]].claimed_in
        if printed is None or (claimed_in is not None and claimed_in.amount == doc.total):
            continue   # none printed; or on a claim, whose line for it confirms what it books (part of a bill, say)
        first = rows[doc.rows[0]]
        if first.document_type == "agent_statement":
            net = doc.total if doc.direction == "in" else -doc.total
            if net == printed:
                continue
            message = f"Items net to {_gbp(net)}; the statement paid over {_gbp(printed)}."
        else:
            printed = abs(printed)
            # What the document's excluded rows add to its total, signed as the total is: against its direction, less.
            aside = sum((g if d == doc.direction else -g for d, g in left_out.get(first.document_ref, {}).items()),
                        ZERO)
            if doc.total == printed or doc.total + aside == printed:
                continue
            kind = "claim" if first.document_type == "expense_claim" else (
                _NUMBERED.get(first.document_type) or _KIND_WORDS.get(first.document_type, "document"))
            word = "line" if first.document_type == "expense_claim" else "row"
            gap = printed - doc.total - aside   # what the lines, left out ones included, don't reach
            message = (f"{word.capitalize()}s total {_gbp(doc.total)}; the {kind}'s total is {_gbp(printed)}. "
                       + (f"{_gbp(gap)} missing: add the {word}." if gap > 0
                          else f"{_gbp(-gap)} over: remove a duplicate or fix an amount."))
            changed = [n for n in doc.rows if read[n] is not None and rows[n].gross - read[n] == -gap]
            same = [n for n in doc.rows if rows[n].gross == -gap] if gap < 0 else []
            where = doc.rows[0]
            if len(changed) == 1:
                where = changed[0]
                message = (f"Changed from {_gbp(read[where])} to {_gbp(rows[where].gross)}; {word}s now total "
                           f"{_gbp(doc.total)}, the {kind}'s total is {_gbp(printed)}. Revert if wrong.")
            elif len(same) == 2 and rows[same[0]].date == rows[same[1]].date:
                where = same[1]
                message = f"Repeats another {word} ({_gbp(-gap)}): read twice? Remove it if so."
            elif len(same) == 1:
                where = same[0]
                message = f"{_gbp(-gap)} over the {kind}'s total, this {word}'s amount. Check it belongs."
            # Show its lines: the line it repeats, when it was read twice; otherwise every other line.
            twice = len(changed) != 1 and len(same) == 2 and where == same[1]
            others = [same[0]] if twice else [n for n in doc.rows if n != where]
            at = marked[where]
            marked[where] = at.model_copy(update={"issues": at.issues + [issue("total_mismatch", message,
                                                                               related=others)]})
            continue
        marked[doc.rows[0]] = first.model_copy(update={"issues": first.issues + [issue("total_mismatch", message)]})
    return marked


# The documents that carry a number of their own, which a copy of one repeats, as a message names each.
_NUMBERED = {"receipt": "receipt", "invoice": "invoice", "expense_claim": "expense claim"}


def _number(tx: Transaction) -> str:
    """A document number as printed, whatever its spacing, punctuation or case: "SC 2026 00718" is "SC-2026-00718"."""
    return re.sub(r"[^0-9A-Z]", "", (tx.document_number or "").upper())


def _without_copies(rows: list[Transaction]) -> list[Transaction]:
    """The rows, each row of a document that repeats an earlier one marked as its copy: the same kind of document
    with the same number, from the same counterparty, the same way and for the same total, such as a reminder of
    a bill or a receipt photographed twice (a receipt on the same day too: till numbers start again each day).
    A copy is not booked unless a person ticks Include."""
    numbered = _grouped(rows, lambda tx: tx.document_type in _NUMBERED and bool(_number(tx)))
    originals: list[_Document] = []
    marked = list(rows)
    for doc in sorted(numbered.values(), key=lambda d: (d.date or dt.date.max, d.ref)):
        first = rows[doc.rows[0]]
        original = next((o for o in originals if _repeats(first, doc, rows[o.rows[0]], o)), None)
        if original is None:
            originals.append(doc)
            continue
        found = issue("duplicate_document", f"Same {_NUMBERED[first.document_type]} as {original.label()}, no. "
                                            f"{first.document_number}. Not booked; tick Include if it's another.",
                      "info")
        for n in doc.rows:
            marked[n] = rows[n].model_copy(update={"copy_of": original.settlement(original.total),
                                                   "issues": rows[n].issues + [found]})
    return marked


def _repeats(tx: Transaction, doc: _Document, earlier_tx: Transaction, earlier: _Document) -> bool:
    return (tx.document_type == earlier_tx.document_type and _number(tx) == _number(earlier_tx)
            and doc.direction == earlier.direction and doc.total == earlier.total
            and names_match(doc.counterparty, earlier.counterparty)
            and (tx.document_type != "receipt" or doc.date == earlier.date))


def _grouped(rows: list[Transaction], keep) -> dict[str, _Document]:
    """The rows `keep` picks, grouped into documents by document_ref. A claim is dated by its latest dated line.
    An agent's statement is the agent's, and goes the way its net does: in when the agent owes the business."""
    documents: dict[str, _Document] = {}
    for n, tx in enumerate(rows):
        if not keep(tx):
            continue
        ref = tx.document_ref or f"row-{n}"
        who = tx.agent or tx.counterparty
        doc = documents.setdefault(ref, _Document(ref=ref, kind=tx.document_type, direction=tx.direction.value,
                                                  account=tx.contra_account_code, counterparty=who,
                                                  description=f"{tx.agent}'s statement" if tx.agent
                                                  else tx.description))
        doc.rows.append(n)
        doc.total += tx.gross if tx.direction.value == doc.direction else -tx.gross
        if tx.date and (doc.date is None or tx.date > doc.date):
            doc.date = tx.date
        doc.counterparty = doc.counterparty or who
    for doc in documents.values():
        if doc.total < 0:
            doc.direction, doc.total = ("in" if doc.direction == "out" else "out"), -doc.total
        doc.open = doc.total
    return documents


def _another(line: Transaction, doc: _Document) -> bool:
    """Still open, the same direction, and another document: an item of an agent's statement pays a bill or a claim,
    never an agent's statement, which only the bank line of its net pays."""
    return (doc.open > ZERO and doc.direction == line.direction.value and doc.ref != line.document_ref
            and not (line.document_type == "agent_statement" and doc.kind == "agent_statement"))


def _could_pay(line: Transaction, doc: _Document) -> bool:
    """Another open document the same way, and the payment dated on or after it, however long after. A payment of
    exactly what is owed clears the document whenever it comes; other payments also need to be within PART_WINDOW."""
    return (_another(line, doc) and doc.date is not None and line.date is not None
            and doc.date <= line.date)


def _recent(line: Transaction, doc: _Document) -> bool:
    """The payment dated within PART_WINDOW of the document it could pay."""
    return line.date <= doc.date + PART_WINDOW


def _paid_before(line: Transaction, doc: _Document) -> bool:
    """Another open document the same way, and the payment dated in the 31 days before it (a card payment or an
    advance). Such a payment is only ever suggested, never matched automatically."""
    return (_another(line, doc) and doc.date is not None and line.date is not None
            and line.date < doc.date <= line.date + WINDOW)


def _apply_link(n: int, line: Transaction, documents: dict[str, _Document], change: dict) -> Optional[list[Issue]]:
    """A person's decision on a bank line. None when it settles the line (Unlink, or Link to documents still in
    the table); otherwise the issues so far, and the line is matched automatically."""
    if line.link == []:                                   # Unlink: an ordinary bank line
        return None
    if line.link is None:
        return []
    linked = [documents[ref] for ref in line.link if ref in documents]
    if len(linked) < len(line.link):
        return [issue("stale_link", "Linked document gone; matched automatically.")]
    found: list[Issue] = []
    if len({d.account for d in linked}) > 1:              # one bank line posts against one account
        found.append(issue("mixed_link", "Linked documents are on different accounts. Link each to its own bank "
                                         "line.", "error"))
    else:
        _pay(n, line, linked, change, found)              # the person's choice, as given
    _note(change, line, found)
    return None


def _exact(line: Transaction, docs: list[_Document]) -> list[_Document]:
    """The documents surely from the line's counterparty (same_payee) with exactly the line's amount open on them."""
    return [d for d in docs if line.counterparty and same_payee(line.counterparty, d.counterparty)
            and d.open == line.gross]


def _outcome(line: Transaction, documents: dict[str, _Document]) -> tuple[str, list[list[_Document]], str]:
    """What _settle does with an automatic line: its kind (pay, choose, suggest, open_document or none), the
    documents it concerns, and why a suggestion or open_document was not a match."""
    kind, options = _decide(line, [d for d in documents.values() if _could_pay(line, d)])
    why = "the names do not match"
    if kind == "none":
        early = [d for d in documents.values() if _paid_before(line, d) and d.open == line.gross]
        if early:
            kind, options, why = "suggest", [[d] for d in early], "the payment is dated before the document"
    if kind == "none":     # never silent: someone this line is paid to or from has an open document
        open_documents = [d for d in documents.values() if _another(line, d) and line.date and line.counterparty
                          and names_match(line.counterparty, d.counterparty)]
        if open_documents:
            kind, options = "open_document", [[d] for d in open_documents]
            why = _why_not_matched(line, open_documents)
    return kind, options, why


def _settle(n: int, line: Transaction, documents: dict[str, _Document], change: dict, found: list[Issue]) -> None:
    kind, options, why = _outcome(line, documents)
    if kind == "pay":
        _pay(n, line, options[0], change, found)
    elif kind == "choose":
        found.append(issue("choose_payment", f"Could pay: {_choices(options)}. Choose one.", "error"))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    elif kind == "open_document":
        found.append(issue("open_document", f"May pay {_choices(options)}: not matched because {why}. Link it if "
                                            "so."))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    elif kind == "suggest":
        found.append(issue("possible_payment", f"May pay {_choices(options)}: same amount, but {why}. Link it if "
                                               "so."))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    _note(change, line, found)


def _why_not_matched(line: Transaction, docs: list[_Document]) -> str:
    """Why a line to someone with these open documents pays none of them: the first reason that applies."""
    if not any(d.open == line.gross for d in docs):
        return "the amount differs"
    if line.date and any(d.date and line.date > d.date + PART_WINDOW for d in docs):
        return "it is more than 120 days after the document"
    return "the payment is dated before the document"


def _decide(line: Transaction, docs: list[_Document]) -> tuple[str, list[list[_Document]]]:
    """What to do with a bank line, given the open documents it could pay (same direction, dated on or before it):
    pay them, ask a person to choose, suggest them, or nothing. Only documents surely from the same counterparty
    (same_payee) are ever paid automatically, and only those within 120 days unless they owe exactly what the line
    pays. A document whose name only resembles the line's is suggested (_outcome), for a person to Link."""
    named = [d for d in docs if line.counterparty and same_payee(line.counterparty, d.counterparty)]
    exact = _exact(line, named)
    if exact:
        return ("pay", [exact]) if len(exact) == 1 else ("choose", [[d] for d in exact])
    named = [d for d in named if _recent(line, d)]
    docs = [d for d in docs if line.date <= d.date + WINDOW]
    sets = _sets_adding_up_to(line.gross, named)
    if sets:
        return ("pay", sets) if len(sets) == 1 else ("choose", sets)
    if named:      # a part payment or an overpayment, when only one document can be meant
        return ("pay", [named]) if len(named) == 1 else ("choose", [[d] for d in named])
    same_amount = [d for d in docs if d.open == line.gross]
    return ("suggest", [[d] for d in same_amount]) if same_amount else ("none", [])


def _sets_adding_up_to(amount: Decimal, docs: list[_Document]) -> list[list[_Document]]:
    """Sets of two to five documents owed on the same account (a bank line posts against one account)
    whose open amounts add up to the payment, oldest documents first; stops once it has found more than
    five, since a person has to choose anyway."""
    found: list[list[_Document]] = []
    for account in sorted({d.account or "" for d in docs}):
        oldest = sorted((d for d in docs if (d.account or "") == account), key=lambda d: (d.date, d.ref))[:20]
        for size in range(2, MAX_SET + 1):
            for chosen in combinations(oldest, size):
                if sum((d.open for d in chosen), ZERO) == amount:
                    found.append(list(chosen))
                    if len(found) > 5:
                        return found
    return found


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
                                      description=f"{line.agent}'s statement" if line.agent else line.description,
                                      kind=line.document_type))
    still_owed = owed_before - line.gross
    if still_owed > ZERO:
        found.append(issue("part_payment", f"Paid {_gbp(line.gross)} of {_gbp(owed_before)}; {_gbp(still_owed)} "
                                           "still owed."))
    elif still_owed < ZERO:
        found.append(issue("overpayment", _overpaid(line, docs[-1], -still_owed)))
    change.update(paid_against=docs[0].account, pays=pays, vat_posted=ZERO, net=line.gross)


def _overpaid(line: Transaction, doc: _Document, excess: Decimal) -> str:
    whom = {DEBTORS: "the customer", STAFF_EXPENSES: "the employee"}.get(doc.account, "the supplier")
    if line.direction == Direction.IN:
        return f"Received {_gbp(excess)} over. You owe {whom} {_gbp(excess)}."
    return f"Paid {_gbp(excess)} over. {whom[0].upper()}{whom[1:]} owes you {_gbp(excess)}."


def _choices(options: list[list[_Document]]) -> str:
    return " or ".join(" + ".join(d.label() for d in option) for option in options)


def _note(change: dict, line: Transaction, found: list[Issue]) -> None:
    if found:
        change["issues"] = line.issues + found
