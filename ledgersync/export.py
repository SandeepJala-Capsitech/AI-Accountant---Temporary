"""A client's work as an Excel workbook (design of 2026-10-06): its trial balance and its transactions, from
the same ledger the pages show. Amounts are numbers in pounds and dates are dates, so the sheets add up."""
from __future__ import annotations

import datetime as dt
import io
import re
from decimal import Decimal
from typing import Optional, Union

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from .models import Direction, Ledger, Transaction, TrialBalance

MONEY = '"£"#,##0.00'
DATE = "dd/mm/yyyy"
COLUMNS = ("Date", "Description", "Counterparty", "Document type", "In/Out", "Amount", "VAT", "Net", "Account",
           "Account name", "Other side", "Still owed", "Issues", "Upload", "Edited", "Left out")
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
    if not isinstance(balance, list):   # the statements are worked from the trial balance: none while it is stopped
        _statements(book, ledger, balance)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _append(sheet, values) -> None:
    """Adds a row. Text read from documents stays text: characters a worksheet can't hold are dropped, and
    text starting with "=" is not made a formula."""
    sheet.append([ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in values])
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str) and cell.value.startswith("="):
            cell.data_type = "s"


def _title(sheet, text: str) -> None:
    _append(sheet, [text])
    sheet["A1"].font = Font(bold=True)


def _widths(sheet, widths) -> None:
    for n, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + n)].width = width


def _trial_balance(sheet, ledger: Ledger, balance, day: dt.date) -> None:
    sheet.title = "Trial balance"
    _title(sheet, f"{ledger.client.name} — trial balance — {ledger.year.label}, as at {day.day} {day:%b %Y}")
    if isinstance(balance, list):
        _append(sheet, ["The trial balance can't be produced yet:"])
        for problem in balance:
            _append(sheet, [problem])
        return
    _append(sheet, ["Code", "Account", "Debit", "Credit"])
    for line in balance.lines:
        _append(sheet, [line.code, line.name, line.debit, line.credit])
    _append(sheet, [None, "Totals", balance.total_debits, balance.total_credits])
    for row in sheet.iter_rows(min_row=3, min_col=3, max_col=4):
        for cell in row:
            cell.number_format = MONEY
    sheet.freeze_panes = "A3"
    _widths(sheet, (8, 34, 14, 14))


def _statements(book, ledger: Ledger, balance: TrialBalance) -> None:
    """A "Profit and loss" sheet and a "Balance sheet" sheet, from the trial balance, as the Trial balance page lays
    them out: income less cost of sales (the 5000s) and overheads; fixed assets (below 1000) and current assets less
    liabilities, equal to capital and reserves with the year's profit. Suspense is on the side its balance is on."""
    def net(line) -> Decimal:
        return line.debit - line.credit

    def block(sheet, title: str, lines, sign: int) -> Decimal:
        total = Decimal("0.00")
        for line in lines:
            _append(sheet, [line.code, line.name, sign * net(line)])
            total += sign * net(line)
        if lines:
            _append(sheet, [None, title, total])
            sheet.cell(row=sheet.max_row, column=2).font = Font(bold=True)
        return total

    def finish(sheet) -> None:
        for row in sheet.iter_rows(min_row=2, min_col=3, max_col=3):
            row[0].number_format = MONEY
        _widths(sheet, (8, 40, 16))

    def total_row(sheet, title: str, amount: Decimal) -> None:
        _append(sheet, [None, title, amount])
        sheet.cell(row=sheet.max_row, column=2).font = Font(bold=True)

    used = [line for line in balance.lines if net(line) != 0]
    pl = book.create_sheet("Profit and loss")
    _title(pl, f"{ledger.client.name} — profit and loss — {ledger.year.label}")
    income = block(pl, "Total income", [l for l in used if l.type == "income"], -1)
    cost_of_sales = [l for l in used if l.type == "expense" and l.code.startswith("5")]
    direct = block(pl, "Cost of sales", cost_of_sales, 1)
    if cost_of_sales:
        total_row(pl, "Gross profit", income - direct)
    overheads = block(pl, "Overheads", [l for l in used if l.type == "expense" and not l.code.startswith("5")], 1)
    profit = income - direct - overheads
    total_row(pl, "Net loss" if profit < 0 else "Net profit", profit)
    finish(pl)

    def is_asset(line) -> bool:
        return net(line) > 0 if line.code == "9998" else line.type == "asset"

    bs = book.create_sheet("Balance sheet")
    _title(bs, f"{ledger.client.name} — balance sheet — {ledger.year.label}")
    fixed = block(bs, "Fixed assets", [l for l in used if is_asset(l) and l.code < "1000"], 1)
    current = block(bs, "Current assets", [l for l in used if is_asset(l) and l.code >= "1000"], 1)
    owed = block(bs, "Liabilities", [l for l in used if l.type == "liability" and not is_asset(l)], -1)
    total_row(bs, "Net assets", fixed + current - owed)
    equity = block(bs, "Capital brought forward and paid in or out", [l for l in used if l.type == "equity"], -1)
    _append(bs, [None, "Loss for the year" if profit < 0 else "Profit for the year", profit])
    total_row(bs, "Capital and reserves", equity + profit)
    finish(bs)


def _transactions(sheet, ledger: Ledger) -> None:
    uploads = {u.id: u.name for u in ledger.uploads}
    _title(sheet, f"{ledger.client.name} — transactions")
    _append(sheet, COLUMNS)
    for tx in ledger.transactions:
        code, name = (tx.paid_against, tx.paid_against_name) if tx.paid_against else (tx.account_code, tx.account_name)
        _append(sheet, [tx.date, tx.description, tx.counterparty, _TYPE_NAMES.get(tx.document_type, tx.document_type),
                      "In" if tx.direction == Direction.IN else "Out", tx.gross, tx.vat_posted, tx.net, code, name,
                      tx.contra_account_code, tx.owed, "; ".join(i.message for i in tx.issues) or None,
                      uploads.get(tx.upload_id), "Yes" if tx.edited else None, _left_out(tx)])
    for row in sheet.iter_rows(min_row=3):
        row[0].number_format = DATE
        for cell in (*row[5:8], row[11]):
            cell.number_format = MONEY
    sheet.freeze_panes = "A3"
    _widths(sheet, (12, 36, 22, 16, 8, 12, 10, 12, 9, 26, 10, 12, 50, 24, 8, 20))


def _left_out(tx: Transaction) -> Optional[str]:
    """Why a row the sheet lists is not in the books: a decision left it out, or replaced it with the rows of a split
    (which the sheet lists too). None for a row in the books, so adding up the rest adds up the books."""
    return "Replaced by a split" if tx.superseded_by else "Left out" if tx.excluded else None
