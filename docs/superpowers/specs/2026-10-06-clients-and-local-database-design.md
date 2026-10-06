# Clients and a Local Database — Design

Date: 2026-10-06 · Branch: `robust-ledger` · Parts 1–3 approved in conversation on 2026-10-06.

## Goal

Turn LedgerSync from a one-page scratchpad into a small tool for an accountant's clients:

- The first page lists the clients (companies). Each has a responsible person, and can be added, edited,
  archived and restored.
- Each client keeps its analysed documents and their rows in a local database, so work survives closing
  the browser.
- The UI gets the "Ledger" look, with a light/dark switch.
- Four things an accountant needs before trusting the saved figures:
  - rows can be corrected;
  - VAT is right on entertainment, cars and a limited company's director;
  - bank statements are checked against their own balances;
  - a client's work exports to Excel.

**Success:** you add "Business Cube Ltd, a limited company, responsible person Jenny Clarke", upload its
invoices and bank statement, and fix the one row the model got wrong. The statement shows "Balances add
up". You close the browser. The next day everything is still there, and Export to Excel gives the trial
balance and the transactions.

## Decisions (the user's answers, 2026-10-06)

- The app calls them **clients**. "Account" keeps its meaning: a ledger account such as 1200.
- The **responsible person** is the client's own contact (its director or owner), with an email and a
  phone number.
- A client also records whether it is **VAT registered**, and its **business type**.
- **Removing a client archives it.** An archived client is hidden from the list and can be restored.
  Clients are never deleted for good.
- **Only the extracted rows are saved**, not the original files. An upload keeps the file's name and its
  SHA-256 fingerprint, never its content.
- **Storage is SQLite inside the API**, through Python's built-in `sqlite3`. No new packages.
- **The look is "Ledger"**, with a light/dark switch.
- **Removing an upload** (a duplicate, say) deletes its rows for good, after you confirm.
- **From the accountant review:** add editing rows, the VAT and chart fixes, the statement balance check
  and Excel export now (part 3).
- **Left for later designs:**
  - year end and periods;
  - VAT scheme and the VAT return;
  - a missing paperwork list;
  - profit and loss, and the balance sheet;
  - aged debtors and creditors;
  - imports into accounting software;
  - opening balances and journals;
  - remembered corrections;
  - change history and period locks;
  - backups.

## Assumptions (stated, not asked)

- One user on this machine, with no logins. The API keeps listening on 127.0.0.1 only, so the client data
  is reachable from this computer alone.
- When analysing for a client, the business name is the client's company name: it tells the model whose
  books these are. `LEDGERSYNC_BUSINESS_NAME` stays as the fallback for analyses without a client, which
  is how the eval runs.
- Saving is automatic. There is no Save button.
- Two clients may have the same name. The list shows each one's contact, which tells them apart.

## Part 1 — What is saved, and the API (approved 2026-10-06)

### Storage

- The database is one file. `LEDGERSYNC_DB_PATH` sets it; the default is `data/ledgersync.db` in the
  project folder, next to `server.py`. The file and its folder are created on first start, and `data/`
  is added to `.gitignore`.
- A new module, `ledgersync/store.py`, holds a `Store` class over `sqlite3`:
  - it opens one connection per call;
  - it turns foreign keys on;
  - it uses the WAL journal, so a background job saving rows and a page reading them don't block each
    other;
  - it marks the schema version with `PRAGMA user_version = 1`, creates missing tables on start, and
    leaves a later version room to migrate from 1.
- Times are stored as UTC ISO-8601 strings.

### Tables

```
clients (id, name, business_type, contact_name, contact_email, contact_phone, vat_registered,
         archived_at, created_at, updated_at)
uploads (id, client_id → clients, name, kind, sha256, model, warnings, created_at)
rows    (id, client_id → clients, upload_id → uploads ON DELETE CASCADE, position, data)
```

- `business_type` is one of `limited_company`, `sole_trader`, `partnership` or `llp`.
- `archived_at` is empty for an active client.
- `kind` is the intake kind (`text`, `table`, `pdf`, `image`), or `manual` for a manual entry.
- `sha256` is the fingerprint of an uploaded file's bytes. It is empty for pasted text and manual entries.
- `warnings` is the JSON list of warnings the analysis returned.
- `position` keeps a document's rows in the order the model gave them.
- `data` is the JSON of the row's inputs (next section).
- `clients.updated_at` changes whenever the client, one of its uploads or one of its rows changes. The
  clients page shows it as "Updated".

### What a saved row holds

- **Saved:** the inputs of `Transaction`:
  - date, description, direction, gross, vat, vat_treatment;
  - account_code, contra_account_code, currency;
  - source, method, evidence;
  - document_type, counterparty, document_ref;
  - include, link;
  - balance, opening_balance, closing_balance (part 3).

  Also saved:
  - the issues the ledger does not work out itself, that is the codes not in `checks._DERIVED`, such as
    the adapter's check that a document's rows add up to its total;
  - after a row's first edit, `original`: the model's values of the fields that can be edited
    (part 3).
- **Never saved:** the outputs (vat_posted, net, paid_against, pays, candidates, owed, paid_by and the
  derived issues).
- **On every read:** the rows are run through `normalise`, then `match`, then the statement check, with
  the client's settings. So statuses can't go stale, and changing a client's VAT setting or business type
  re-books every saved row.

### A client's settings

- Validation, matching and the trial balance for a client use
  `BusinessSettings(business_name=client.name, vat_registered=client.vat_registered,
  business_type=client.business_type)`.
- Analyses for that client tell the model the client's name and business type.
- Without a client, `business_type` is empty, and the chart is offered as today (part 3).

### API

All endpoints are under `/api` and speak JSON. Their models live in `ledgersync/models.py`, and CORS
also allows `PATCH`.

| Method and path | Body | Returns |
|---|---|---|
| `GET /clients?archived=false` | — | Client summaries sorted by name: the active ones, or the archived ones with `archived=true` |
| `POST /clients` | `name`, `business_type`, `contact_name`, `contact_email`?, `contact_phone`?, `vat_registered`? (default true) | 201 and the client |
| `GET /clients/{id}` | — | The client |
| `PATCH /clients/{id}` | Any client field, and `archived` (true or false) | The client |
| `GET /clients/{id}/ledger` | — | The ledger: the client, its uploads (newest first, each with its row count, `sha256` and statement check) and its rows, matched, each with `id`, `upload_id`, `edited` and `original` |
| `PATCH /clients/{id}/rows/{row_id}` | `link` and/or `include`; or the fields to edit; or `revert: true` | The ledger |
| `POST /clients/{id}/uploads` | `name`, `kind` (`manual`), `transactions` | 201 and the ledger |
| `DELETE /clients/{id}/uploads/{upload_id}` | — | The ledger |
| `GET /clients/{id}/trial-balance` | — | The trial balance of the saved, booked rows; 422 when it can't be posted, as today |
| `GET /clients/{id}/export.xlsx` | — | The Excel workbook (part 3) |
| `POST /analyze` (existing) | New optional form field `client_id` | As today; a finished job's result also carries `client_id` and `upload_id` |

- **A client summary** is the client plus three figures: `rows` (how many are saved), `to_review` (rows
  with an error or a warning after matching and the statement check) and `updated_at`.
- **`include`** on a row ticks or unticks Include on every row of its document (the same
  `document_ref`), as the page does today.
- **`link`** is accepted on bank lines (`document_type` `statement`) only. On any other row it is a 422.
  `link: null` returns the line to automatic matching; leaving `link` out leaves it unchanged.
- **Renaming a client** changes analyses from then on. Rows already read keep their direction.
- **An archived client** can only be restored (`archived: false`). Any other change to it is a 409.
- **Removing an upload:** if its documents were linked by a bank line, that line now shows the existing
  `stale_link` warning.

### Analysing for a client

When `POST /api/analyze` is given a `client_id`:

1. **Before the job is queued:** an unknown client is a 404, and an archived client a 409 ("Restore
   Business Cube Ltd to add documents").
2. **While the job runs:** the extractor is told the client's name and business type, and the adapter
   uses the client's settings.
3. **When the job finds at least one row:** the server saves an upload, then puts `client_id` and
   `upload_id` in the job's result. The upload holds:
   - the file's name, or "Pasted text";
   - its kind and the file's SHA-256;
   - the model and the warnings;
   - its rows, in order.
4. **When the job finds no rows, fails or is cancelled:** nothing is saved.
5. **If the client is archived while its job is running:** the save still happens.

Without a `client_id`, `/api/analyze` saves nothing and works as today, apart from part 3's VAT and chart
fixes and the balances the model now reads. The eval uses it that way, and so does manual entry when it
asks the model for an account.

### Manual entry

1. The page asks `/api/analyze`, without a `client_id`, to suggest an account.
2. It builds the row from what was typed, as today.
3. It saves the row with `POST /clients/{id}/uploads`, with the name "Manual entry" and the kind `manual`.

### Errors

All errors keep today's shape, `{"detail": {"code", "message"}}`.

- **404:** an unknown client, row or upload, or a row or upload that belongs to another client.
- **409:** a change to an archived client, other than restoring it. This covers analyse, uploads, row
  changes and removing an upload.
- **422:**
  - a blank company name or responsible person;
  - an email without "@";
  - an unknown business type;
  - `link` on a row that isn't a bank line;
  - a manual upload with no rows;
  - an edit that fails part 3's checks.
- **500 `storage_error`:** the database file can't be opened or written. The log names the path; it
  never logs document contents.

### Unchanged

`/api/accounts`, `/api/health` and the eval runner work as they do today. `/api/transactions/validate`
and `/api/trial-balance` keep their requests and responses, and pick up part 3's VAT and chart fixes.

## Part 2 — Pages and the Ledger look (approved 2026-10-06)

### The clients page, `/`

- **Header:** LedgerSync, the AI status, and the light/dark switch.
- **Title:** "Clients", with how many are active and how many archived.
- **"Add client"** opens a dialog:
  - Company name (required);
  - Business type (limited company, sole trader, partnership or LLP; it starts on limited company);
  - Responsible person (required), Email and Phone;
  - VAT registered, a switch that starts on.

  Its errors show next to their fields: "Enter the company name", "Enter the responsible person",
  "Enter a valid email".
- **Search box:** filters by company, responsible person or email as you type.
- **Table:** sorted by company name, with these columns:
  - Company, with its business type underneath;
  - Responsible person, with the email and phone underneath;
  - VAT ("Registered" or "Not registered");
  - Rows;
  - To review;
  - Updated.

  Clicking a row opens the client.
- **Each row's actions:**
  - Edit opens the same dialog, filled in.
  - Archive asks first: "Archive Business Cube Ltd? You can restore it from Show archived."
- **"Show archived"** lists the archived clients, each with Restore.
- **With no clients yet:** "Add your first client", with the button.

### A client's page, `/clients/[id]`

- **Breadcrumb:** Clients / {name}.
- **Header:**
  - the client's name, business type and VAT badge;
  - the responsible person, with email and phone;
  - Edit details, Export to Excel, and Archive.
- **Figures:**
  - Rows;
  - To review;
  - To pay: what is still owed on documents with money going out, such as bills, expense claims and
    credit notes to customers;
  - To receive: what is still owed on documents with money coming in, such as sales invoices and
    suppliers' credit notes.
- **Add documents:** today's Upload, Paste and Manual entry, saved to this client.
  - Several files at once works as now (the API's `max_parallel_jobs`), with today's progress, Cancel and
    Retry.
  - A picked file whose fingerprint matches one of this client's saved uploads is skipped: "Skipped:
    same file as BT-0905.pdf, uploaded 6 Oct".
- **Transactions:** today's table, loaded from the API and saved to it.
  - It keeps the statuses, Link, Unlink, None of these, Match automatically, Include, the duplicate
    warnings, Net, and the totals with what is still owed.
  - Each row gets Edit, and an edited row shows "Edited" with Revert (part 3).
  - Each row's origin is its upload's name.
  - "Clear table" goes away.
- **Uploads:**
  - each upload's name, kind, rows, date and warnings;
  - for a bank statement, its check: "Balances add up", "Doesn't add up: £12.40", or "No balances to
    check";
  - Remove, which asks first: "Remove BT-0905.pdf and its 1 row? This can't be undone: analyse the file
    again to get them back."
- **Trial balance:** "Generate trial balance" builds it from the saved rows and shows it as today.
- **An archived client:**
  - a banner says "Archived. Restore to add documents or make changes", with Restore;
  - Add documents, Edit, Link, Unlink, Include and Remove are hidden;
  - Export to Excel still works.
- **An unknown client id:** "Client not found", with a link back to Clients.

### The Ledger look

- **Colours** are tokens in `globals.css`. The light set sits on `:root`; the dark set sits on
  `[data-theme="dark"]`, and on `prefers-color-scheme: dark` unless the page is `[data-theme="light"]`.

  | Token | Light | Dark |
  |---|---|---|
  | Page | `#FAF8F3` | `#151714` |
  | Card | `#FFFFFF` | `#1D201C` |
  | Text | `#1F2421` | `#ECEAE3` |
  | Muted text | `#5F5E5A` | `#A3A199` |
  | Rule | `#E4E0D6` | `#30342E` |
  | Accent | `#0F6E56` | `#1D9E75` |
  | Accent tint / text on it | `#E1F5EE` / `#085041` | `#085041` / `#9FE1CB` |

  Warning, error and success keep their meanings (amber, red and green) in both modes.
- **Type:**
  - headings in Source Serif 4;
  - text in Inter;
  - figures (amounts, dates and account codes) in IBM Plex Mono, right-aligned with tabular numbers.

  All three load through `next/font`.
- **Layout:** thin rules rather than heavy boxes, and at most one accent-filled button in a view.
- **The switch:**
  - the page starts on the computer's setting;
  - the switch flips between light and dark and remembers the choice in this browser (`localStorage`,
    key `ledgersync-theme`);
  - a small script in the layout applies the choice before the page paints, so it doesn't flash.
- **Width:** pages work in a window 1024 px wide without the page scrolling sideways. The transactions
  table may scroll inside its card on narrower windows.

### Code layout

- `src/app/page.tsx` becomes the clients page.
- `src/app/clients/[id]/page.tsx` is a client's page.
- `src/components/` holds `AppHeader` (with the theme switch), `ClientDialog`, `AddDocuments`,
  `TransactionsTable`, `EditRowDialog`, `UploadsList` and `TrialBalancePanel`.
- `src/lib/clients.ts` has the calls for clients, ledgers, rows, uploads and trial balances. In
  `src/lib/api.ts`, `analyze` gains a client id.
- `src/lib/ledger.ts`, already tested, stays as it is.
- The new pure helpers (client search, the dialogs' checks and the four figures) get node tests, and
  `npm test` runs every `src/lib/*.test.ts`.
- If the frontend-design skill is installed, it guides the components' details within these tokens.

## Part 3 — What an accountant needs before trusting the figures (approved 2026-10-06)

### Edit a row

- **The form.** Edit on a row opens a form with these fields:
  - date (or none);
  - description (required);
  - counterparty;
  - money in or out;
  - amount (above zero);
  - VAT shown (none, or at least zero and below the amount);
  - account, from the accounts the client's business type can use;
  - document type, one of the ten.
- **Whole document or one row.** Counterparty and document type change for every row of the document
  (the same `document_ref`), as Include does. The other fields change only that row.
- **New references.** A row without a `document_ref` that becomes any type other than `statement` gets a
  new reference of its own.
- **Keeping the original.** On a row's first edit, the store keeps the model's values of those fields as
  `original`; later edits leave `original` alone. The ledger returns `edited` and `original`. The table
  shows "Edited", with the original values on hover.
- **Revert.** `revert: true` puts back the original values and clears `original`. Manual entries keep
  what was typed as their original.
- **Saving.** Edits go through `PATCH /clients/{id}/rows/{row_id}`. The ledger re-books and re-matches at
  once: an invoice changed to a receipt stops being owed, and fixing a misread amount on a statement line
  clears its balance warning.
- **Errors.** A failed check is a 422 that names the field, for example "VAT must be below the amount".

### VAT and chart fixes

These apply everywhere, including analyses without a client and `/api/transactions/validate`.

- **An account can block VAT.** `Account` gains `reclaim_vat` (true by default). On a money-out row
  posted to an account that blocks VAT, the ledger books no VAT: the VAT shown stays in the cost. The
  row gets the info issue `vat_blocked`: "VAT on Entertainment can't be reclaimed, so the £6.37 stays in
  the cost."
- **7403 Entertainment** blocks VAT. Its definition adds that VAT on client entertainment can't be
  reclaimed.
- **0050 becomes "Cars"** and blocks VAT, defined as: "Cars bought for the business; VAT on a car can't
  be reclaimed unless it is used only for business."
- **New 0055 "Vans"** (an asset, standard rate, VAT reclaimed): "Vans and other commercial vehicles
  bought for the business."
- **New 2250 "Director's Loan Account"** (a liability, outside the scope of VAT): "Money a director has
  lent the company, or taken from it for personal use. Limited companies only."
- **Accounts by business type.** The accounts a person or the model can choose depend on it:
  - `choosable(business_type)` for a limited company leaves out 3260 Drawings and 3000 Capital
    Introduced;
  - for a sole trader, partnership or LLP, and when there is no client, it leaves out 2250.
- **The model's prompt** names the business type ("a UK limited company") and lists
  `choosable(business_type)`.
- **Rows on the wrong account for the business type** get a warning:
  - for a limited company, a row on 3260 or 3000 gets `director_loan`: "For a limited company, use 2250
    Director's Loan Account instead of Drawings";
  - for the other types, a row on 2250 gets the same code, `director_loan`: "Director's Loan Account is
    for limited companies; use 3260 Drawings or 3000 Capital Introduced."
- **Derived issues.** `vat_blocked` and `director_loan` join `checks._DERIVED`, so they are worked out
  afresh on every read and never saved.

### Statement balance check

- **What the model reads.** It gains three fields, which are null when not printed, and always null on
  rows that aren't bank statement lines:
  - `balance`: the running balance printed on the line, after it;
  - `opening_balance`: the statement's opening balance (brought forward), on every row;
  - `closing_balance`: its closing balance (carried forward), on every row.

  Rule 5 of the prompt says to give them, and that balance lines are still not transactions. The adapter
  carries them into `Transaction` as inputs.
- **The check.** A new module, `ledgersync/statements.py`, checks the statement rows of each upload, in
  their saved order, on every ledger read:
  1. **Running balances:**
     - The rows that show a balance are compared one to the next, adding the amounts of any rows between
       them that show none. Some statements print the balance once a day.
     - Both readings are tried: oldest first (each balance is the one before plus this row), and newest
       first. The one that fits more pairs is used.
     - Where a balance doesn't follow, that row gets the warning `statement_gap`: "The balance after this
       line should be £1,240.50; the statement shows £1,252.90."
  2. **Opening and closing:**
     - The opening balance is the first one given in the upload, and the closing balance the last.
     - If both are known, opening plus money in, minus money out, must equal closing.
     - If not, the last statement row gets `statement_total`: "Opening £5,000.00 plus these rows gives
       £4,987.60, but the statement closes at £5,000.00 (£12.40 apart)."
  3. **The upload's summary** is in the ledger: `ok`, `gap` with the difference, or `none` when no
     balances were printed.
- **Precision.** Amounts are compared to the penny, with `Decimal`.
- **Severity.** Both are warnings, so they count towards "To review" but don't block the trial balance.
  Both join `checks._DERIVED`.
- **Mixed uploads.** Only statement rows are checked.
- **Eval.** After the prompt change, re-run the eval's statement and table cases (their generated
  statements carry balances) and its receipts, and report row accuracy. The new fields add a few output
  tokens per statement row.

### Excel export

- **The endpoint.** `GET /api/clients/{id}/export.xlsx` returns the client's workbook:
  - its file name is `{client name} {YYYY-MM-DD}.xlsx`, with characters that file names can't hold
    removed;
  - a new module, `ledgersync/export.py`, builds it with openpyxl (already installed) from the same
    ledger the page shows.
- **"Trial balance" sheet:**
  - a title row ("Business Cube Ltd — trial balance — 6 Oct 2026");
  - the columns Code, Account, Debit and Credit, one row per line, and a totals row;
  - when the trial balance can't be produced yet, the title, then "The trial balance can't be produced
    yet:", and one problem per row. The export never fails for that reason.
- **"Transactions" sheet:**
  - one row per saved row, in the table's order;
  - the columns: Date, Description, Counterparty, Document type, In/Out, Amount, VAT, Net, Account,
    Account name, Other side, Still owed, Issues, Upload and Edited;
  - amounts are numbers formatted `£#,##0.00`, and dates are dates formatted `dd/mm/yyyy`;
  - the header row is frozen, and issues are joined with "; ".
- **Archived clients** can be exported.
- **On the page,** Export to Excel is a link to the endpoint through the Next proxy, so the browser
  downloads the file.

## Testing

**Backend (pytest, with a temporary database for each test):**

- **The store:**
  - creating, listing, reading, editing, archiving and restoring clients;
  - uploads with their rows in order;
  - outputs are not saved, and issues the ledger doesn't derive are kept;
  - `original` is kept on the first edit only, and cleared by revert;
  - removing an upload removes its rows and nothing else;
  - `updated_at` changes;
  - the schema is created in an empty file;
  - foreign keys hold.
- **The API:**
  - each endpoint, and its 404, 409 and 422 errors;
  - `analyze` with a `client_id`:
    - it saves an upload and its rows;
    - it tells the model the client's name and business type;
    - it applies the client's VAT setting, so a client that isn't VAT registered keeps VAT in the cost
      of its bills;
    - it saves nothing when there are no rows, or the job fails or is cancelled;
  - a bill and its bank payment, saved in two separate uploads, are paired in the ledger;
  - Link and Include re-match the rows;
  - removing an upload whose documents were linked gives `stale_link`;
  - a trial balance per client;
  - `analyze` without a `client_id` saves nothing.
- **Editing:**
  - each field, with its checks;
  - counterparty and document type change across the whole document;
  - a statement row turned into an invoice gets a reference and becomes owed;
  - revert;
  - an archived client's rows are refused.
- **VAT and chart:**
  - an entertainment receipt and a car keep their VAT in the cost, with `vat_blocked`;
  - a van reclaims its VAT;
  - a limited company is offered 2250 and not 3260 or 3000, and the other types the reverse;
  - `director_loan` warnings;
  - without a client, the chart is as today plus Cars and Vans;
  - the eval case that codes client sandwiches to 7403 is checked against blocked VAT, and its expected
    VAT changed only if the eval scores the VAT the ledger books.
- **Statements:**
  - oldest-first and newest-first running balances;
  - balances printed once a day;
  - a skipped line flags the right row with the right amount;
  - opening plus rows not reaching the closing balance;
  - no balances gives `none`;
  - mixed uploads;
  - a fix with Edit clears the warning.
- **Export:**
  - both sheets are there, and the trial balance totals match `GET /trial-balance`;
  - amounts are numbers in £ format and dates are dates;
  - the file name is cleaned;
  - a trial balance that can't be produced lists its problems;
  - an archived client exports.

**Frontend:**

- node tests for client search, the dialogs' checks and the figures;
- `tsc --noEmit` and `next build`;
- a browser check on spare ports (UI 3001 talking to API 8087, with a temporary database):
  - add a client;
  - analyse a bill and a bank statement;
  - reload the page;
  - Link, edit a row and revert it;
  - see the statement's check;
  - export to Excel;
  - remove an upload;
  - archive and restore;
  - switch between light and dark.

## Out of scope (later designs, from the accountant review)

- saving the original files, and viewing or downloading them;
- logins, and several users;
- a staff list, or assigning clients to staff;
- year end, accounting periods, and a trial balance for a date range;
- VAT scheme (cash accounting, flat rate), VAT number and quarter, and the VAT return;
- a missing paperwork list, and queries for the client;
- profit and loss, the balance sheet, and aged debtors and creditors;
- imports into Xero, QuickBooks, Sage or FreeAgent;
- opening balances, journals and a fixed asset register;
- remembered corrections, and bulk changes;
- change history and period locks;
- backups and restore;
- deleting clients for good;
- an ORM or a migration framework.

## README

- Add a "Clients and saved work" section.
- Add `LEDGERSYNC_DB_PATH` to the settings table.
- Add the chart changes: 0050 Cars, 0055 Vans and 2250 Director's Loan Account, with VAT blocked on
  entertainment and cars.
- Describe the Excel export.
