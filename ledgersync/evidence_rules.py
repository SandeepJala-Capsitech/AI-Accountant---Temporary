"""The document rules: what a purchase needs before it can be booked. Under £50 nothing; from £50 to £250 a
receipt or invoice; over £250 a full VAT invoice. A receipt that shows the supplier's VAT number and the VAT
reclaims it (a simplified VAT invoice, up to £250). A row without what it needs waits in Suspense until the
document is uploaded. Only outgoing purchases and expenses are covered; transfers, tax, loans and money in are not.
Pure and deterministic: a row after checks.normalise and matching.match goes in, a verdict comes out."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel

from .accounts import ALL_BANK_CODES, BY_CODE, SUSPENSE, Account, AccountType
from .models import BusinessSettings, Direction, JournalLine, Transaction
from .money import ZERO, VatTreatment
from .posting import journal_for

NO_DOCUMENT_LIMIT = Decimal("50.00")   # below this a purchase needs no document
SIMPLIFIED_LIMIT = Decimal("250.00")   # up to this a receipt will do; above it only a full VAT invoice


class DocumentRule(BaseModel):
    action: Literal["book", "hold"]
    reclaim_vat: bool
    needs: Optional[Literal["receipt_or_invoice", "vat_invoice"]] = None
    reason: str          # one plain sentence, shown on the question card and the decision


def _fixed_asset(account: Account) -> bool:
    """Whether the account holds equipment or vehicles the business buys (0030 to 0055): not their depreciation,
    which is outside the scope of VAT, nor Debtors, Prepayments or the banks."""
    return (account.type == AccountType.ASSET and account.code.startswith("00")
            and account.vat != VatTreatment.OUTSIDE_SCOPE)


def _pays_its_document(tx: Transaction) -> bool:
    """Whether the row pays a document that fits it, which was booked, and checked, with the document: one a person
    linked, or one matching paid for exactly what the document owed. A payment of £50 or more that matching took as
    a part payment or an overpayment doesn't: the document's total differs from the payment, so it isn't the
    payment's document, and the payment waits for its own or for a person's Link."""
    if tx.paid_against is None:
        return False
    return (tx.link is not None or tx.gross < NO_DOCUMENT_LIMIT
            or not any(i.code in ("part_payment", "overpayment") for i in tx.issues))


def _covered(tx: Transaction) -> bool:
    """Whether the rules apply: money out on an expense account or a fixed asset, not one of the client's own
    bank accounts, and not a payment of its document (_pays_its_document)."""
    account = BY_CODE.get(tx.account_code)
    return (tx.direction == Direction.OUT and account is not None
            and (account.type == AccountType.EXPENSE or _fixed_asset(account))
            and tx.account_code not in ALL_BANK_CODES and not _pays_its_document(tx))


def _hold(needs: Literal["receipt_or_invoice", "vat_invoice"], reason: str) -> DocumentRule:
    return DocumentRule(action="hold", reclaim_vat=False, needs=needs, reason=reason)


def document_rule(tx: Transaction, settings: BusinessSettings) -> DocumentRule:
    """What to do with one row after checks.normalise and matching.match: book it (reclaiming its VAT or not),
    or hold it in Suspense for the document it needs. settings is accepted so the rules can follow a client's
    own settings later; none changes them yet."""
    if not _covered(tx):
        return DocumentRule(action="book", reclaim_vat=True, reason="No document is needed for this kind of row.")
    if tx.document_type in ("statement", "expense_claim"):   # a line with no document behind it
        if tx.gross < NO_DOCUMENT_LIMIT:
            return DocumentRule(action="book", reclaim_vat=False,
                                reason="Small purchase: booked from the line without a receipt; no VAT reclaimed.")
        if tx.gross > SIMPLIFIED_LIMIT:
            return _hold("vat_invoice", "No receipt or invoice, and a purchase this size needs a full VAT "
                                        "invoice: held in Suspense until it is uploaded.")
        return _hold("receipt_or_invoice", "No receipt or invoice: held in Suspense until one is uploaded.")
    if tx.document_type == "receipt":
        if tx.gross > SIMPLIFIED_LIMIT:
            return _hold("vat_invoice", "Only a receipt, but this needs a full VAT invoice: held in Suspense "
                                        "until it is uploaded.")
        if tx.not_vat_invoice:
            return DocumentRule(action="book", reclaim_vat=False, reason="Not a VAT invoice: VAT kept in the cost.")
        if not (tx.vat_posted or ZERO) > 0:
            return DocumentRule(action="book", reclaim_vat=False, reason="Receipt shows no VAT: nothing to reclaim.")
        if tx.supplier_vat_number is None:
            return DocumentRule(action="book", reclaim_vat=False,
                                reason="Receipt without the supplier's VAT number: VAT kept in the cost.")
        return DocumentRule(action="book", reclaim_vat=True,
                            reason="Receipt shows the supplier's VAT number and the VAT: VAT reclaimed.")
    if tx.document_type == "invoice":
        if tx.not_vat_invoice:
            return DocumentRule(action="book", reclaim_vat=False, reason="Not a VAT invoice: VAT kept in the cost.")
        return DocumentRule(action="book", reclaim_vat=True, reason="Invoice: VAT reclaimed.")
    # Any other kind of row (a payroll line, an agent's statement) has no rule of its own: booked as normalise left it.
    return DocumentRule(action="book", reclaim_vat=True, reason="No document is needed for this kind of row.")


def journal_with_rule(tx: Transaction, index: int, rule: DocumentRule) -> list[JournalLine]:
    """Journal lines for one row after the rule: as posting.journal_for when it books and reclaims, the whole
    gross on the account (no VAT leg) when it books without, and the gross against Suspense when it is held.
    The row is not changed."""
    if rule.action == "book" and rule.reclaim_vat:
        return journal_for(tx, index)
    if rule.action == "book":   # the VAT stays in the cost
        return journal_for(tx.model_copy(update={"vat_posted": ZERO, "net": tx.gross}), index)
    out = tx.direction == Direction.OUT   # money out: Suspense takes the debit, the contra the credit
    lines = [JournalLine(transaction=index, code=SUSPENSE, description=tx.description,
                         debit=tx.gross if out else ZERO, credit=ZERO if out else tx.gross),
             JournalLine(transaction=index, code=tx.contra_account_code, description=tx.description,
                         debit=ZERO if out else tx.gross, credit=tx.gross if out else ZERO)]
    debits, credits = sum(l.debit for l in lines), sum(l.credit for l in lines)
    if debits != credits:   # one gross either side; this guards the invariant
        raise AssertionError(f"transaction {index} does not balance: {debits} != {credits}")
    return lines
