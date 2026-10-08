# Unpaid Documents and Payment Matching — Design

**Date:** 2026-10-06 · **Status:** for review · **Builds on:** commit `ea96414` plus the uncommitted fixes of 2026-10-06 (PDF text read with PyMuPDF, one row per expense-claim line, 1200 restored as Bank Current Account, 1100 Debtors and 2100 Creditors added)

## Goal

Book documents the way an accountant would (accruals). A bill the business has received but not paid
is money owed to the supplier, not money out of the bank. When the payment later appears on a bank
statement, it clears what was owed instead of being booked as a second expense.

Prompted by a rent invoice (Business Cube Management Solutions, October 2026, £12,000 including
£2,000 VAT) that was received but not paid. Today every document is booked as paid from 1200
Bank Current Account.

**Success:**
- The unpaid rent bill posts Rent £10,000 Dr, Purchase VAT £2,000 Dr, **2100 Creditors £12,000 Cr**.
- Its bank payment posts **2100 Creditors £12,000 Dr, 1200 Bank £12,000 Cr**. Creditors is back to
  nil and the rent is counted once.
- Matt Barnes's claim (24 lines, £3,542.26) is owed on **2110 Expenses Owed to Staff** until a bank
  line of £3,542.26 to "M BARNES" clears it.
- Two rent bills of the same amount and one payment: the trial balance waits for a person to choose.
- A part payment or an overpayment is applied and flagged.
- A pro forma invoice or a supplier statement is not booked unless a person includes it.
- The eval keeps 100% correct rows on the cases it runs, and also reports how often the model gets the
  document type right.

## Decisions and assumptions

| | Decision | Source |
|---|---|---|
| 1 | Accruals: unpaid documents go to control accounts, and bank lines clear them | user |
| 2 | Every invoice starts unpaid, including one that shows £0.00 due. Only till and card receipts count as paid when issued. | user |
| 3 | Expense claims are owed to the employee until reimbursed | user |
| 4 | Matching runs in the API with the ledger code, not in the browser | user |
| 5 | Debtors or Creditors is decided by the counterparty side (from the account), not by the direction of the money, so credit notes post correctly | accountant review, user |
| 6 | Claims use their own liability account, 2110 Expenses Owed to Staff, not trade Creditors | accountant review, user |
| 7 | A payment is matched only when dated on or after the document and **at most 31 days** after it | user |
| 8 | When several documents could be the one paid, a person chooses; the trial balance waits | user |
| 9 | Part payments **and overpayments** are applied and flagged | user |
| 10 | Quotes, pro formas, purchase orders, remittance advices and supplier statements are not booked unless a person includes them | user |
| 11 | The model never chooses 1100, 2100 or 2110; the code does, from the document type | design |
| 12 | Bookings stay recalculable: the model's output and the person's decisions are inputs, everything matching works out is an output, recomputed on every call (the same rule `checks.normalise` already follows) | design |

## Part 1 — Document types and booking (approved 2026-10-06)

The model reports two new fields on every row (`ledgersync/extractor.py`, `AccountingTransaction`):

- `document_type`: one of `receipt`, `invoice` (credit notes included), `expense_claim`, `statement`,
  or a document that is not a transaction: `quote`, `pro_forma`, `purchase_order`,
  `remittance_advice`, `supplier_statement`.
- `counterparty`: who was paid or who paid, as printed (supplier, customer, employee or payee).

The prompt gains one sentence per type, for example: "A till or card receipt was paid when it was
issued. An invoice or bill asks for payment, even when it says paid. A credit note is recorded like
an invoice, with the money going the other way."

The ledger picks the other side of each row (`ledgersync/checks.py`, where `normalise` sets
`contra_account_code` today):

| Document type | Other side |
|---|---|
| `receipt` | 1200 Bank Current Account (as now) |
| `invoice`, row on an income account (4000 Sales, 4900 Other Income) | 1100 Debtors |
| `invoice`, any other account | 2100 Creditors |
| `expense_claim` | 2110 Expenses Owed to Staff (new account in `ledgersync/accounts.py`, liability, not offered to the model) |
| `statement` | 1200 Bank, or the control account of the document it pays (Part 2) |
| not-a-transaction types | not booked; booked as an `invoice` when a person ticks **Include** |

- An explicit `contra_account_code` sent by an API client still wins, as today.
- The adapter (`ledgersync/adapter.py`) gives the rows of one document a shared `document_ref` (a
  short random id). Each statement line gets its own. Rows are grouped into a document the way
  `_documents` groups them now (same input, same `document_total`); a document without a total is one
  row.
- An unexpected `document_type` from the model is treated as not-a-transaction, so a person checks it.
- Manual entries in the UI are `receipt`s (paid), as now.

## Part 2 — Matching payments (approved 2026-10-06, with the user's edits)

New module `ledgersync/matching.py`: a pure function from all the table's transactions to the same
transactions with matching outputs filled in. `POST /api/transactions/validate` and
`POST /api/trial-balance` both run `normalise` then `match`, so the table and the trial balance agree.

**Open documents:** every booked `invoice` and `expense_claim` (and every included not-a-transaction
document). Each has a counterparty, a direction, a total (the sum of its rows' gross) and a date (its
own; for a claim, its latest line's date). What is still open is the total minus the payments applied
to it.

**Bank lines:** rows of type `statement`. Lines the person has linked are applied first; the rest are
taken in date order, oldest first, so the result is the same whatever order files were uploaded in. A bank line pays a document only if it goes the same way (money
out pays a bill, money in pays a sales invoice or settles a supplier credit note) and is dated on or
after the document and at most 31 days after it. Undated rows are neither matched nor suggested; they keep the existing "No date found" warning.

**Rules for each bank line**, first match wins:

1. **The person's decision** (`link`, see below) is applied as given.
2. **One exact match: applied.** Same counterparty, same amount to the penny, and exactly one open
   document fits.
3. **Several exact matches** (for example monthly rent): **not applied.** Error issue
   `choose_payment`: "Could pay: Rent invoice 1 Oct (£12,000.00) or Rent invoice 1 Nov (£12,000.00).
   Choose one." Error issues already block the trial balance (`posting.trial_balance` raises
   `InvalidTransactions`).
4. **Several documents, one payment: applied** when exactly one set of 2 to 5 open documents from
   the counterparty adds up to the payment; more than one set is treated like rule 3.
5. **Part payment: applied and flagged.** Same counterparty, less than the open amount, exactly one
   candidate. Warning `part_payment`: "Paid £10,000.00 of £12,000.00. £2,000.00 still owed."
6. **Overpayment: applied and flagged.** Same counterparty, more than the open amount, exactly one
   candidate. Warning `overpayment`: "Paid £50.00 more than owed. The supplier now owes you £50.00."
   For a customer: "Received £50.00 more than owed. You now owe the customer £50.00." The excess
   stays on the control account (Creditors goes into debit, or Debtors into credit).
7. If rules 5 or 6 find more than one candidate from the counterparty, it is treated like rule 3.
8. **Same amount, names do not match: suggestion only.** Warning `possible_payment`, with a Link
   option. The bank line is booked as now (against the model's account) until linked.
9. Otherwise the bank line is booked as now.

**Names match** when, after lower-casing, removing punctuation, legal words (ltd, limited, plc, llp,
co, the) and bank words (bank, payment, fin, card, dd, so, bacs, fps, ref) and numbers, they share a
word of four or more letters, or a word of three or more letters is an abbreviation of a word in the
other name (same first letter, letters in the same order). "M BARNES EXPENSES" matches "Matt Barnes";
"BUS CUBE MGMT" matches "Business Cube Management Solutions".

**Booking a matched bank line** (`ledgersync/posting.py`): the bank line posts against the control
account of the document(s) it pays (2100, 2110 or 1100), with no VAT, because the VAT was booked with
the document. Its own `account_code` from the model is kept as an input, so unlinking restores it.

**The person's decisions**, inputs stored on the bank line or the document row and sent with every call:

- `link: list[str] | None` on a bank line. `None` means automatic, `[]` means not a payment of any
  document (**Unlink**), and a list of `document_ref`s means "pays exactly these" (**Link**).
- `include: bool` on a not-a-transaction document, `False` by default (**Include** tick). It applies to
  the whole document: every row sharing its `document_ref`.
- A link to a document that is no longer in the table is dropped with a warning, and the line is
  matched automatically.

**Outputs, recomputed on every call:**

- On a bank line: `pays` (the documents it pays and how much of each), and `candidates` (the options
  behind a `choose_payment` or `possible_payment` issue, for the Link buttons).
- On a document row: `owed` (what is still open on its document), and `paid_by` (the bank lines that
  paid it, with dates).
- Matching's issue codes join `checks._DERIVED`, so validating again replaces them instead of
  adding copies.

**Not covered:** a payment for a document that is not in the table (for example last year's bill) is
booked as now. Opening balances are a separate design.

## Part 3 — The table, the trial balance, errors and tests (approved 2026-10-06)

**Table** (`frontend/src/app/page.tsx`, `frontend/src/lib/api.ts`):

- A status line under each description, where the file name sits now:
  - documents: "Unpaid, owed to Business Cube", "Paid by bank line on 3 Oct" or "Part paid: £2,000.00
    still owed";
  - bank lines: "Pays: Business Cube rent invoice" **[Unlink]**, "Could pay: Rent 1 Oct / Rent 1 Nov"
    **[Link] [Link]**, or "May pay …" **[Link]**;
  - not-a-transaction documents: greyed, "Looks like a pro forma, not booked" **[☐ Include]**.
- The Account column shows where the row is posted; a matched bank line shows 2100 Creditors.
- After every upload and every Link, Unlink or Include, the page sends all rows to
  `/api/transactions/validate` and shows what comes back.
- The duplicate check (`frontend/src/lib/duplicates.ts`) skips a pair made of a document and a bank
  line, because matching handles those. Receipt and bank line, or the same bill uploaded twice, are
  still flagged.
- Totals (replacing "Total money in / out"), each with Amount, VAT and Net, shown when not empty:
  **Paid out (bank)** and **Received (bank)** for rows posted against the bank; **Owed by you** (bills,
  claims and customer credit notes) and **Owed to you** (sales invoices and supplier credit notes) for
  documents. Rows not booked are left out.

**Trial balance:** 1100 Debtors, 2100 Creditors and 2110 Expenses Owed to Staff show what is still
owed. An unresolved `choose_payment` blocks it with the existing 422 message, which lists the rows.
Warnings do not block it.

**Errors:**
- A model answer without `counterparty`: the row cannot match automatically; rules 8 and 9 still apply.
- A model answer with an unknown `document_type`: treated as not-a-transaction (Part 1).
- Rows sent to the API by older clients, without `document_type`: treated as `receipt`, so existing
  API behaviour does not change.

**Tests:**
- Unit tests for every matching rule: exact match, ambiguity, several documents, part payment,
  overpayment, the 31-day limit, undated rows, name matching (including the two examples above), link,
  unlink, a stale link, recomputing without duplicate issues, and upload order not changing the result.
- Booking tests: a bill, a supplier credit note, a sales invoice, a customer credit note, a claim, an
  included pro forma, and a matched payment (journal lines and trial balance).
- API tests: `/api/transactions/validate` and `/api/trial-balance` return the same matches; an
  ambiguous payment returns 422.
- Prompt tests: the instructions describe every document type and the new fields (the existing
  pattern in `tests/test_extractor.py`).

**Eval** (`eval/`):
- Label every existing fixture's expected rows with `document_type`, and score it as a new field in
  `eval_scoring.py` (counterparty is not scored).
- New fixtures for documents that are not transactions: a pro forma invoice and a supplier statement
  of account.
- Matching scenarios (an unpaid bill and its payment, two rent bills and one payment, a part payment)
  are deterministic code, so they are unit tests, not eval cases: the eval sends one document per case.
- Run the affected eval cases on the free plan; the full eval (about a day's free allowance) when there
  is room.

## Out of scope (from the accountant review, for later designs)

- Opening balances (bank, Debtors, Creditors), so bills from before the period are not booked again.
- Receipts paid in cash (Petty Cash 1230) or on a personal card (a claim, or the director's loan account).
- VAT schemes: under the VAT Cash Accounting Scheme, VAT counts when paid. This matters for a VAT
  return, not for the trial balance.
- Invoices already paid by card, which stay in Creditors unless the bank statement is uploaded too.
- Separate fixes spotted on the way: VAT on client entertainment is not reclaimable in the UK, but
  7403 Entertainment books it; mileage claimed at 55p a mile is above HMRC's 45p rate (a payroll matter).

## Effect on the roadmap

This replaces the cash-basis assumption in the robustness design ("every document is booked as paid
from the bank"). Phase 4's hybrid extraction is unaffected, but its bank parsers will need to report
`document_type: statement` and a `counterparty`.
