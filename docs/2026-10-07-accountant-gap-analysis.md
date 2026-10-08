# Super Accountant: what an accountant needs, and where the gaps are

**Date:** 7 October 2026 · **Based on:** the code on `robust-ledger` (60 commits, specs and plans in
`docs/superpowers/`), the eval results in `eval/results/`, and this week's tests with two real-shaped clients
(Mint Asset Management Ltd, two years of a property company; Jenny Hogg's expense claim with its receipts).

**Assumed user:** a UK practice accountant (as at Acting Office) preparing year-end work for small clients:
limited companies, sole traders and landlords. The end products are year-end accounts, a corporation tax or
self-assessment return, and VAT returns. Correct this if the target is different (see the question at the end).

---

## 1. The verdict in one page

**What it is today.** A strong engine for reading documents and booking them correctly. It reads receipts,
invoices, credit notes, expense claims, agents' statements and bank statements (PDF, scans, photos, CSV, Excel,
pasted text). It books them on accruals, with double entry and UK VAT rules. It matches payments to bills, a
receipt to its card payment and a receipt to its claim line, and sets aside copies and reminders. Totals and
statement balances are checked, and the result is a balanced trial balance a person can review and correct.
The bookkeeping is right when the inputs are complete. The Mint and Jenny trial balances matched an
accountant's figures to the penny once the documents were in.

**What it isn't yet.** It isn't somewhere an accountant can *finish* a client. It stops at an unadjusted trial
balance of the movements it was given:

- for one bank account;
- with no accounting period, and no opening balances;
- with no journals, and no year-end adjustments;
- with no evidence behind each row (the source files aren't kept, and the bank's own wording is rewritten);
- with no reports beyond the trial balance, and no VAT return;
- with no logins;
- with an export nothing else can import.

**The five gaps that matter most**, in order:

1. **Period and opening balances.** Without them the trial balance can never agree with last year's accounts
   or with the bank's closing balance. Mint's bank showed £736.45 *credit* when the account held £50.44.
2. **Adjustments.** There are no journals, prepayments, accruals or depreciation. Every year end needs these.
   Mint alone needed three prepayments and deferred income of £6,149.49.
3. **Evidence and audit trail.** Original files, the bank's original narrative, and who changed what. A
   reviewer can't sign off rows they can't trace back to a document.
4. **Remembered judgement.** The model codes the same payee differently on different runs ("First Essex" went
   to Travel, then to Fuel). Nothing remembers a person's correction, so every month is reviewed from scratch.
5. **Data protection and logins.** Documents go to OpenRouter or Groq with no processing agreement, and anyone
   who can reach the API can change any client.

---

## 2. The accountant's job, stage by stage, against what exists

| Stage | What the accountant does | Super Accountant today | Gap |
|---|---|---|---|
| 1. Collect | Statements for every month and account, invoices, receipts, claims, payroll, loan and asset papers, last year's accounts and TB | Upload many files at once; skips a file seen before; reads everything with one model | No **missing paperwork list** (months with no statement, bank lines with no document, an agent's missing months); no client portal or email-in |
| 2. Code | Code each bank line and document; spot private spending, capital items and disallowables | Account chosen from a Sage-style chart by what was bought; Suspense when unsure; VAT as printed | No **rules** or remembered corrections; one fixed chart for every client; no bulk recode; disallowables (clothing, fines, entertainment for tax) not marked |
| 3. Match | Bills to payments, receipts to card lines, claims to reimbursements | Strong: names, amounts, 31-day window, sets of up to five, part and over payments, choose or suggest; receipts to card lines; claim lines to receipts; agents' statements; copies | Bank fees taken off a payment, FX, and payments for documents from before the period aren't handled |
| 4. Reconcile | Bank to statement at year end; debtors, creditors, DLA and VAT control | Statement rows checked against printed balances; still owed shown per document | No **bank reconciliation** report; one bank account only; no transfers between the client's own accounts; no aged debtors or creditors; no DLA ledger |
| 5. Adjust | Opening balances, accruals, prepayments, deferred income, depreciation, stock, tax, dividends | None | **Opening balances, journals, prepayments and accruals, fixed assets**: all missing |
| 6. Review | Preparer and reviewer, queries to the client, comparison with last year | Issues shown on each row; a review bar by kind of issue; Edit and Revert | No **queries to the client** (accountants keep a "query sheet"; Mint's had ten lines); no sign-off; no change history; no comparison with last year |
| 7. Produce | P&L, balance sheet, notes (FRS 105 or FRS 102 1A), CT computation and CT600, VAT return through MTD, SA103 or SA105 | Trial balance; Excel export (trial balance and transactions) | No **P&L or balance sheet**, no VAT return, no tax figures |
| 8. Hand off | Into production software (IRIS, CCH, Taxfiler, Xero, QuickBooks, Sage, FreeAgent) and the practice system | Excel only | No **TB or journal export** in any of their import formats; no Acting Office integration |

---

## 3. What has been built (and how well)

### Reading documents
- **Inputs:** PDF (text layer first, page images for scans, at most three per request); photos (turned upright
  and resized); CSV, Excel and pasted text; manual rows.
- **Model:** Qwen `qwen3.8-27b`, through OpenRouter when its key is set, otherwise Groq. It answers in a
  strict JSON schema with per-row validation, so one bad row doesn't sink a batch.
- **Jobs:** background jobs with progress and cancel, two documents at a time.
- **Document types:**
  - receipts, invoices and credit notes, expense claims, bank statements and agents' statements;
  - quotes, pro formas, purchase orders, remittances and supplier statements, which are read but not booked.
- **What each row carries:** counterparty, document number and printed totals. Receipts with mixed items are
  split by account only when their VAT can be divided.
- **Accuracy (eval):** 31 synthetic documents. The latest run (18 of the cases, 149 expected rows) has 100%
  correct rows (amount, date, direction) and 97.3% account accuracy. All of it is synthetic: there's no real-document
  eval yet (`eval/private` exists but isn't scored).

### Booking
- **Chart:** a Sage 50-style chart of about 50 accounts, each with a plain-words definition for the model.
  Owners use the director's loan account (limited companies) or drawings and capital (everyone else). 4904 Rent
  Income was added this week.
- **Double entry:** accruals on the right control account: Creditors, Debtors, Expenses Owed to Staff, and the
  agent's balance on Debtors.
- **VAT:**
  - Reclaimed only when shown. Not reclaimed on entertainment or cars, and none for a client that isn't VAT
    registered.
  - 5% and £0.00 are accepted. A claim showing VAT its receipt doesn't is flagged.
- **Trial balance:** always balances; rows with errors block it.

### Matching and checks (the strongest part)
- **Bank lines and documents:**
  - Bank lines pay invoices and claims. An exact payment wins over a bigger one, a single payment can clear
    several bills, and part payments and overpayments are flagged. When it isn't sure, a person chooses or
    gets a suggestion.
  - Names match even when they're short, like "HML PM Ltd".
- **Pairing:**
  - Receipts with their card lines, and claim lines with their receipts or invoices. The document is preferred
    and the claim line greyed out.
  - Agents' statements: rent grossed up, the agent's fees and the bills it paid booked, and the net cleared by
    the bank.
- **Live checks:**
  - Copies (a reminder with the same number; the same receipt photographed twice) aren't booked.
  - Every document is checked live against its printed total, once, showing the amount missing.
  - Bank statements are checked against their printed balances.

### Review screen
- **Clients and uploads:** a client list with archive; uploads, with remove.
- **Table:** each row says what it pays or owes. Link, Unlink and Include; Edit and Revert; Remove; Add
  transaction and Add line.
- **Issues:** shown in words on each row, plus a review bar that filters by kind of issue. Possible-duplicate
  flags too.
- **Totals and output:** totals by bank and documents, "To pay" and "To receive", a trial balance panel, and
  the Excel export.

### Engineering
- 566 backend and 35 frontend tests, and an eval harness with resume, rescore and per-field scoring.
- Local SQLite, an API bound to localhost, and a header check that stops other web pages from posting.

---

## 4. What the two test clients showed

Every item below was seen in practice this week. They show where an accountant would stop trusting the
numbers.

| Seen | What it shows | Gap area |
|---|---|---|
| Mint was set up as VAT registered and wasn't. £152.26 of VAT was reclaimed with no warning | A client setting silently changes the whole TB. Nothing checks it against the documents (no VAT number on any invoice to Mint) | Coding, client setup |
| Two years in one client; nothing brought forward | TB can't tie to the workings (freehold £128,971.52, DLA, share capital, reserves) | Period, opening balances |
| Rent in April 2026 covered January to December 2026; the service charge covered April to September | £6,149.49 deferred income and £562.82 of prepayments must be posted by hand, elsewhere | Adjustments |
| "First Essex" (a bus company) became Fuel; the same agent fee went to 7901 on one statement and 7603 on the next | The same payee is coded differently on different runs | Remembered judgement |
| The claim spreadsheet's 13th line was skipped | Even with checks, the reader can miss lines; the total check caught it | Accuracy |
| Model-written descriptions replaced "R+R PR LTD CL AC / ref: 22TELECOM LESS 48" | The bank's own narrative, which a reconciliation and an auditor rely on, isn't kept | Evidence |
| The 2024-25 workbook had June and July out of order: "Doesn't add up £6.15" | The statement check is strict about row order | Bank reconciliation |
| The working papers carried an "Account head" column, so the model copied the accountant's answers | Spreadsheet inputs can leak answers into the coding, which also skews any accuracy test | Accuracy |
| The rent months without an agent's statement stayed net (£806.75, not £925) | The app can't ask for what's missing; it books what it's given | Collection, queries |
| Mint's own "Query sheet" listed ten items to ask the client | The accountant's real workflow has queries; the app has Suspense | Review |

---

## 5. Gap register

**Severity:**
- **Blocker:** can't finish a client's year end without it.
- **High:** the work is slow or risky without it.
- **Medium:** expected by accountants.
- **Low:** nice to have.

**Effort (rough, for this codebase):**
- **S:** days.
- **M:** one to two weeks.
- **L:** several weeks.

### A. Period and completeness
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| A1 | **Accounting period per client** (year end; trial balance for a date range; years kept apart; last year shown beside this year) | Every output is "for the year ended"; `BusinessSettings` already has `period_start` and `period_end`, but no client sets them | Blocker | M |
| A2 | **Opening balances**: type them, or import last year's TB (bank, debtors, creditors, DLA, fixed assets, capital, reserves); last year's unpaid bills open for matching | TB ties to the prior year; bank equals the statement; payments of last year's bills stop being booked as new expenses | Blocker | M |
| A3 | **Year-end close**: lock the period, move the profit to reserves, roll forward | Stops changes to a filed year; starts the next one from the right balances | High | M |
| A4 | **Missing paperwork list**: months or accounts with no statement, breaks in the balance chain (one statement's closing to the next one's opening), bank lines over a threshold with no document, an agent's missing months | Collection is most of the work; it's an obvious place for AI to help | High | S–M |

### B. Adjustments
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| B1 | **Manual journals**: many lines, balanced, with a narrative, optionally reversing next period | Accruals, reclassifications, DLA, tax and dividends all need them | Blocker | M |
| B2 | **Prepayments, accruals and deferred income**: spot documents whose period runs past the year end (they already print "1/4/2026–30/9/2026") and propose the split | Every year end; Mint needed three | High | M |
| B3 | **Fixed asset register and depreciation**: capital or revenue on purchases over a threshold, depreciation policy, disposals, capital allowances | Accounts and tax both need it; 0030, 0040, 0050 and 0055 exist but nothing depreciates them | High | M–L |
| B4 | **Corporation tax, dividends and DLA**: tax provision, dividend journals, overdrawn DLA (s455) and beneficial loan warnings | Limited companies, most of the client base | Medium | M |
| B5 | **Disallowable expenses for tax**: entertainment, clothing, fines and private use marked as disallowed, apart from the VAT rules | The CT computation adds them back | Medium | S |

### C. Evidence and audit trail
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| C1 | **Keep the original files**; click a row to see its page, ideally the region read | A reviewer, HMRC or an auditor asks "show me the receipt" | Blocker | M |
| C2 | **Keep the bank's original narrative** next to the model's description (the unused `evidence` field could hold it) | Bank reconciliation, queries and audit use the bank's wording | High | S |
| C3 | **Change history**: who edited, linked, included or removed what, and when | Professional standards and reviewer sign-off; Remove is permanent today | High | M |
| C4 | **Undo for Remove** (remove for now; delete for good later) | One wrong click loses a row with no record | Medium | S |

### D. Bank and reconciliation
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| D1 | **Several bank and card accounts per client** (current, savings, credit card, petty cash, PayPal, Stripe), with **transfers between them** matched, not booked as spending | Most clients have more than one; today everything is 1200 | High | M |
| D2 | **Bank reconciliation report** at any date: ledger balance against the statement, and what's unreconciled | A standard year-end working paper | High | S–M |
| D3 | **Parsers for bank exports** (CSV, OFX and the known bank layouts; Phase 4 of the 28 Sep plan): exact amounts, offline, no model cost; the model only codes | Long statements are the costliest and slowest reads; parsing is exact | High | M |
| D4 | **Tolerances**: bank fees taken off a receipt, FX payments, a payment that lands days later | Real payments rarely match to the penny | Medium | S–M |
| D5 | **Bank feeds** (Open Banking) | Removes statement collection altogether | Low (later) | L |

### E. Coding consistency and learning
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| E1 | **Rules and remembered corrections**: payee to account and VAT treatment, learned from edits and kept per client (and across the practice) | Consistency month to month; less review each time; AI that learns from the accountant | Blocker for repeat use | M |
| E2 | **A chart per client**: add or rename accounts (service charge, ground rent, letting fees for a property company), map them to the production software's codes | Clients aren't alike; exports need the mapping | High | M |
| E3 | **Bulk changes**: select rows, then recode, link or remove | A 105-line statement is reviewed in bulk | Medium | S |
| E4 | **Why this account**: a short reason and a confidence for each row, sorted for review | Lets a reviewer check the doubtful rows, not every row | Medium | M |
| E5 | **Client setup checks**: "invoices addressed to this client show no VAT number; is it really VAT registered?"; "rent income: has the client opted to tax?" | A wrong setting silently skews the TB (Mint) | Medium | S |

### F. VAT
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| F1 | **VAT details per client** (number, scheme, quarters) and a **VAT return** (the 9 boxes) for a quarter | VAT clients file four times a year | High | M |
| F2 | **MTD submission** (direct, or through bridging software) | The legal route for filing | Medium | L |
| F3 | **Schemes**: cash accounting, flat rate, annual | VAT counts at a different time, or at a fixed percentage | Medium | M |
| F4 | **Property VAT**: option to tax, exempt rent, partial exemption | Landlords and property companies, like Mint | Medium | M |
| F5 | **Reverse charge and imports**: construction (CIS), overseas services, postponed import VAT | Common for trades and e-commerce | Low–Medium | M |

### G. Reports and outputs
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| G1 | **P&L and balance sheet** with comparatives; a **nominal ledger** (drill into any account); **aged debtors and creditors**; DLA and VAT control reports | The working papers an accountant files | Blocker | M |
| G2 | **Exports production software imports**: TB and journals for IRIS, CCH, Taxfiler, Xero, QuickBooks, Sage and FreeAgent, through the chart mapping (E2) | Most practices finish accounts in production software; this is the realistic hand-off | Blocker (if handing off) | S–M each |
| G3 | **Statutory accounts, CT600 and iXBRL** | Better left to production software unless the product aims to replace it | Low (decision) | L |
| G4 | **Self-assessment schedules**: SA103 (sole trader), SA105 (property, including section 24 finance costs) | Sole traders and landlords | Medium | M |

### H. Practice workflow and the client
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| H1 | **Queries to the client**: build the query list from Suspense, missing documents and doubtful rows; send it, and record the answers against the rows | How accountants actually work (Mint's query sheet) | High | M |
| H2 | **Users, logins and roles** (preparer, reviewer), client assignment, sign-off, a job status per client | Several staff share clients; reviewers need sign-off | Blocker (before shared use) | M–L |
| H3 | **Client upload** (portal or email-in) | Saves the accountant collecting and re-sending files | Medium | L |
| H4 | **Notes** on clients and rows | Context for the next person, or next year | Low | S |
| H5 | **Acting Office integration**: clients, contacts and jobs synced; outputs filed back | No re-keying between systems | Medium | M |

### I. Data protection and running it for real
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| I1 | **Processing terms with the model provider** (OpenRouter or Groq): a DPA, UK or EU routing, zero retention; or a private model | Client financial data is personal data (UK GDPR); a practice's engagement letters and its professional body expect it | Blocker (before real client data at scale) | S–M (decision and settings) |
| I2 | **Security**: logins (H2), encryption at rest, backups and restore, retention and deletion for good, audit logs | Today it's one local SQLite file with no access control | Blocker (before hosting) | M |
| I3 | **Hosting and licences**: from localhost to a server; PyMuPDF is AGPL; check the model's licence | Commercial use | High (before launch) | M |
| I4 | **Cost and limits**: cost per client, rate-limit handling, a budget per job | Statements are large; free plans throttle | Medium | S |

### J. Accuracy
| # | Gap | Why it matters | Sev. | Effort |
|---|---|---|---|---|
| J1 | **An eval on real, anonymised documents**, and a gate in CI | Synthetic 100% doesn't predict real documents; this week found misses the eval didn't | High | M |
| J2 | **More checks**: the same payee on the same account, dates inside the period, the bank lines' total against the statement's totals, and claims against reimbursements | Every check turns a silent error into a visible one | Medium | S each |
| J3 | **A second reading for doubtful documents** (or a different model) | Catches skipped lines and misread amounts | Medium | M |
| J4 | **Foreign currency** (rows are refused today) | Overseas suppliers and travel | Medium | M |

### K. Kinds of business with their own needs
- **Landlords and property companies:**
  - a rent schedule per property and tenant, deposits, service charge reconciliations;
  - section 24 for individuals;
  - all of each year's agent statements (to gross up every month).
- **Construction (CIS):** deductions on sales and purchases, monthly returns, the reverse charge (F5).
- **E-commerce:** marketplace and payment-provider payouts net of fees (Amazon, Stripe, PayPal, eBay), split
  into sales, fees and refunds.
- **Payroll:** journals from payroll reports (gross pay, PAYE, NI, pensions); 2210 exists, the journal doesn't.
- **Cash businesses:** till Z-reports and cash banked.

---

## 6. Recommended order

**Stage 1. Finish one client's year end, with Mint 2025-26 as the acceptance test.**
- Upload the year's documents and last year's TB. After adjustments, Super Accountant's TB must equal the
  accountant's working TB: Tide £50.44, freehold £128,971.52, DLA, corporation tax £1,803.20, accruals £358.80,
  share capital £1,000, reserves £7,664.89.
- The build:
  - A1 periods; A2 opening balances; B1 journals; B2 prepayments, accruals and deferred income;
  - G1 P&L, balance sheet and nominal ledger;
  - G2 one TB export (whichever production software Acting Office uses most);
  - C2 the bank's narrative.

**Stage 2. Make repeat work fast and trustworthy.**
- E1 rules and remembered corrections, E2 a chart per client, E3 bulk changes.
- C1 original files, C3 change history.
- D1 several accounts and transfers, D2 bank reconciliation, A4 the missing paperwork list, H1 queries to the
  client.

**Stage 3. Before real clients and several staff.**
- I1 processing terms, H2 and I2 logins, security and backups, I3 hosting and licences, J1 a real-document
  eval.

**Stage 4. VAT and tax.**
- F1 VAT returns, F3 and F4 schemes and property VAT, B3 fixed assets, B4 and B5 tax adjustments, G4
  self-assessment schedules, then F2 MTD.

**Later.** Bank feeds, client portal, CIS, payroll journals, e-commerce payouts, Acting Office integration, and
statutory accounts if the product means to replace production software.

---

## 7. Where AI gives this product an edge

These are things accountants want that rules-based tools do badly. They suit this codebase, which already
understands documents:

- **Asking for what's missing** (A4, H1): "no statement for March"; "nine rent payments, two agent
  statements"; "bank line £706.16 to HML has no bill".
- **Year-end spotting** (B2): reading the period printed on a bill and proposing the prepayment.
- **Explaining a row** (E4) and **learning from corrections** (E1), so the second year is mostly checked
  rather than coded.
- **Comparing with last year**: "Repairs up 300%", "no insurance this year".
- **Checking the client's setup against the documents** (E5): VAT status, business type, option to tax.

---

## 8. The decision this depends on

Does the accountant **finish the job in Super Accountant** (reports, VAT, tax), or **hand a checked trial
balance to production software** and Acting Office? The answer moves G1–G3, F2 and B3–B4 up or down the order
above.
