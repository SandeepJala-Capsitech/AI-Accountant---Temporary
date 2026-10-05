# UK LedgerSync — Robustness, Accuracy & Reliability Plan

> **Update 2026-09-29:** for the MVP demo the app reads documents with Groq's hosted `qwen/qwen3.8-27b`, and the local Ollama model and OCR are gone (see `2026-09-29-groq-vision-model-design.md`). Where this spec says documents never leave the machine, that no longer holds.

## Context

You asked for improvements to make the prototype more robust, more accurate and more reliable. I read the code, then probed the live system (local Ollama `qwen2.5vl:3b` and the `.venv`) on 2026-09-28 with realistic UK inputs. The main problems are structural, not cosmetic:

| Probe | Expected | What happened |
|---|---|---|
| Tesco till receipt (TOTAL 12.50, CASH 20.00, CHANGE 7.50) via Qwen | 1 expense £12.50 | 6 rows: items **and** TOTAL booked as *revenue/Sales*; CASH and CHANGE booked as expenses |
| Same receipt via regex fallback | £12.50 | **£26.14** ("2026 14:22" parsed as 26.14); date `None` |
| Barclays CSV, 3 rows (−72.00, +3600.00, −24.50) via Qwen | 3 rows | **Whole batch rejected (422)**: model kept the `-` sign, which violates `amount ge=0` |
| Same CSV via regex fallback | 3 rows | **1 row**: the header line, for £834.56 (a truncated balance) |
| Supplier refund £89.99 (money in) | reduces expense | booked as a new expense |
| Trial balance of the above | balances | Dr 1,567.49 vs Cr 25.00. Only one side of each entry is posted; there is no Bank side |
| Latency | — | 40s cold, 5–14s warm |

**Your decisions:**
- Accounting: double-entry with VAT.
- Inputs: all four types (photos, bank CSV/Excel, bank PDFs, typed text).
- Deployment: runs locally for now.
- Extraction: **hybrid**. Amounts, dates and signs are parsed; the LLM only classifies.
- AI offline: fail clearly; parsers keep working.
- Legacy UI: retire it.

**Intended outcome:**
- Every row carries an amount, date and direction the accountant can trust, plus a record of where it came from and any issues.
- The TB balances by construction.
- Failures are explicit errors, never plausible-looking wrong numbers.
- Accuracy is measured, not guessed.

## Findings (by priority)

| # | Problem | Where |
|---|---|---|
| 1 | TB is single-sided (expense=Dr, revenue=Cr, no Bank/VAT legs), so it almost never balances | `server.py:206-242` |
| 2 | The prompt has no business perspective and no receipt rules, so line items, totals and tendered cash all become transactions | `qwen_service.py:108-134` |
| 3 | Schema is too narrow: `amount ge=0` with only expense/revenue (refunds, HMRC and loan payments break); `account` is free text, so TB lines fragment ("Utilities" vs "Utility") | `qwen_service.py:17-22` |
| 4 | A silent fallback to the regex parser (proven wrong above) runs whenever Ollama errors, and the UI still labels the output "Qwen AI" | `qwen_service.py:171-173, 175-284` |
| 5 | `async def` endpoint runs blocking OCR, PDF and LLM calls, which freezes the whole server; timeout is 900s | `server.py:118`, `qwen_service.py:54` |
| 6 | `/api/health` raises AttributeError (`model_path` no longer exists) | `server.py:49` |
| 7 | `/` static mount serves the project root, including `.git/`, `.venv/` and source, on `0.0.0.0`; CORS `*` with credentials | `server.py:18-24, 246-251` |
| 8 | Images and PDFs with any OCR/text are sent as text only, so the vision model never sees them; EasyOCR lines lose layout; pdfminer loses table columns | `server.py:57-115, 153-168` |
| 9 | `.xls` (no `xlrd`) and parse errors fall back to decoding binary as UTF-8 and sending it to the LLM; CSVs in cp1252 lose `£`; no size or page limits; encrypted PDFs fail with a misleading message | `server.py:139-150` |
| 10 | `num_predict 2048` truncates long statements (~40 rows); `num_ctx` unset; no `keep_alive` (cold starts); `is_loaded` cached forever | `qwen_service.py:70-89, 157-168` |
| 11 | One invalid row fails the whole batch | `qwen_service.py:287-318` |
| 12 | Manual Entry turns form values into a sentence and lets the LLM re-extract amount and type | `page.tsx:99-106` |
| 13 | Money is held as float and rounded with `round()` (banker's rounding); no VAT anywhere in the new pipeline | `server.py`, `qwen_service.py` |
| 14 | No way to review or edit before the TB; each upload *replaces* the list, so a TB can't span several documents | `page.tsx` |
| 15 | No accuracy measurement: `test_api.py` only checks `success`; `test.jpg` just says "TEST" | `test_api.py` |
| 16 | Operational gaps: unpinned deps, no README, `print` logging that dumps OCR text of client docs, hardcoded `http://localhost:8085` | various |

## Target design

### Backend layout (`server.py` stays as thin FastAPI routes)

```
ledgersync/
  config.py        env settings: OLLAMA_*, HOST, CORS_ORIGINS, MAX_UPLOAD_MB=20, MAX_PDF_PAGES=30
  errors.py        typed errors -> HTTP (413, 415, 422 unreadable, 503 AI offline, 504 AI timeout)
  jobs.py          in-memory job store + 1 worker thread: status, progress ("page 3/12"), cancel,
                   results expire after 1h (fine for local single-user)
  ollama_client.py chat(schema, images); timeout 120s per call; keep_alive 30m; num_ctx 8192;
                   done_reason=="length" -> error; 1 retry; health cached 15s; Semaphore(1);
                   warm-up at startup so the first request isn't a 40s cold start
  intake.py        size cap, magic-byte sniffing, CSV encoding (utf-8-sig -> cp1252), xlsx/xls
                   (all sheets), encrypted-PDF detection; no temp files
  ocr.py           EasyOCR: lock-guarded lazy init, boxes grouped into lines, confidences kept
  accounts.py      UK chart of accounts (Sage 50-style nominal codes)
  money.py         Decimal + ROUND_HALF_UP; VAT split
  models.py        Transaction, Issue, Direction, VatTreatment, ExtractionResult, TrialBalance
  posting.py       transaction -> journal lines -> trial balance
  rules.py         deterministic payee rules applied before the LLM
  tabular.py       bank CSV/Excel statement parser + running-balance reconciliation
  pdf.py           PyMuPDF per page: text(sort=True), find_tables(), scanned-page detection, render
  llm_tasks.py     prompts + LLM-facing schemas (classify_rows, extract_document,
                   extract_statement_rows, extract_freetext)
  checks.py        grounding / arithmetic / date / currency checks -> Issue[]
  pipeline.py      routes each input type (replaces qwen_service.py)
```

### Accounting core
- **Transaction:**
  - `date` is a `datetime.date`; `direction` is `in`/`out` (money into or out of the bank); `gross` is a Decimal > 0; plus `vat`, `net` and `vat_treatment` (standard 20 / reduced 5 / zero / exempt / outside_scope).
  - `account_code` must exist in the chart; `contra_account_code` defaults to `1200` (Bank).
  - `currency`: anything other than GBP is an error-level issue.
  - `source` records the input type; `method` records where the numbers came from (`parsed` / `llm` / `vlm` / `user`).
  - `evidence` holds the verbatim source snippet, and `issues[]` lists anything that needs review.
  - Refunds need no negative amounts; they are expressed through `direction`.
- **Chart of accounts:** about 35 Sage 50-style codes, each with a type (asset/liability/equity/income/expense), a default VAT treatment, and examples used in the LLM prompt. Key codes:
  - Bank and assets: 1200 Bank, 1230 Petty cash, 0030/0040/0050 fixed assets.
  - VAT and liabilities: 2200 Sales VAT, 2201 Purchase VAT, 2202 VAT liability (HMRC payments), 2210 PAYE/NIC, 2300 Loans.
  - Equity: 3000 Capital, 3260 Drawings.
  - Income and costs: 4000 Sales, 4900 Other income, 5000 Purchases.
  - Overheads: 7000 to 8205 (wages, rent, utilities, travel, subsistence, stationery, telephone, legal, accountancy, repairs, interest, bank charges, subscriptions, insurance, refreshments).
  - 9998 Suspense.
- **VAT:**
  - When the business is not VAT-registered, net = gross.
  - When a document shows VAT, that amount wins.
  - Otherwise VAT is calculated from the account's default treatment (standard = gross/6, reduced = gross/21) and flagged "VAT estimated".
  - Every result must satisfy net + VAT = gross.
- **Posting:**
  - `out` → Dr account (net), Dr VAT (vat), Cr Bank (gross).
  - `in` → Dr Bank (gross), Cr account (net), Cr VAT (vat).
  - The VAT leg goes to 2200 for income accounts and to 2201 otherwise. This keeps a supplier refund's VAT in input VAT.
  - Each journal balances (asserted in code).
  - The TB shows each account's net balance (code, name, Dr or Cr) plus totals and the journal.
- **Business settings**, sent with each request: business name (needed to tell sales invoices from purchase invoices), VAT registered, period start/end, bank account code.

### Extraction pipeline (hybrid)

| Input | Numbers come from | LLM role |
|---|---|---|
| CSV / TSV / Excel / pasted table | `tabular.py`: header row found within the first 20 rows (skips bank preamble); column synonyms (Amount, Paid in/out, Money in/out, Debit/Credit, Balance); UK day-first dates; `£`, `,`, `(72.00)`, CR/DR; running-balance check | `classify_rows` in batches of ≤25; result is constrained by a JSON-schema **enum** of account codes |
| PDF with text | `find_tables()` → tabular path; otherwise `extract_statement_rows` or `extract_document` on page text, with evidence checks | classify / extract |
| Scanned PDF / photo | Vision model reads the **image** (EXIF-rotated, downscaled), page by page, with OCR text as a hint; must return a verbatim `total_text` | `extract_document` (receipt/invoice) or `extract_statement_rows` |
| Free text | `extract_freetext` with `amount_text` evidence | extract + classify |
| Manual entry | the form values; the LLM is never used for amounts | `POST /api/classify` suggests an account only |

- **Prompts:**
  - Instructions go in the system message; the document goes in the user message inside `<document>` tags, never interpolated into the instructions.
  - State the business perspective.
  - Receipt rules: one transaction per document, equal to the amount paid; ignore tendered, change and card lines.
  - Include 1–2 few-shot examples, and today's date and the period for dates with no year.
- **`rules.py`** runs before the LLM so the most important postings are always right: HMRC VAT → 2202, HMRC PAYE/NI → 2210, bank charges → 7901, interest → 7900, own-account transfers.
- **`checks.py`** raises these issues:
  - `amount_not_in_source`, `vat_arithmetic`, `vat_rate_mismatch`
  - `date_missing`, `date_out_of_period`, `non_gbp_currency`
  - `balance_mismatch`, `unusual_direction_for_account`
  - `low_ocr_confidence`, `duplicate_in_batch`, `ai_offline_suspense`
- **AI offline:**
  - Photos, scans and free text return 503 with a message saying how to fix it.
  - CSV, Excel and manual entries still import, with accounts set to 9998 Suspense and flagged.
- **API:**
  - `GET /api/health`, `GET /api/accounts`
  - `POST /api/analyze` (multipart file|text + settings; optional `pdf_password`) validates the upload immediately (413/415/422) and returns `{job_id}`
  - `GET /api/jobs/{id}` returns status, progress, and the result or error; `DELETE /api/jobs/{id}` cancels
  - `POST /api/classify`
  - `POST /api/transactions/validate` (the backend owns all VAT maths and checks)
  - `POST /api/trial-balance`
  - Money is serialised as exact strings.

### Frontend (Next.js)
- Split `page.tsx` into `lib/api.ts`, `lib/types.ts`, and the components `InputPanel`, `LedgerTable`, `TrialBalance`, `StatusBar` and `SettingsPanel`.
- `api.ts` uses relative `/api` URLs, polls jobs to show progress and support Cancel, puts an AbortController timeout on every request, and shows `detail` messages rather than raw JSON.
- The ledger **accumulates** across inputs. It is saved in localStorage (wrapped in try/catch) and has a Clear button. Duplicates are flagged by row key and by file SHA-256.
- The table is editable: date, description, direction, gross, VAT, account (dropdown), contra. Edits are re-validated by the backend. Each row shows issue badges and a method badge (Parsed, AI, Vision, You), and a "Needs review" filter is available.
- A health indicator shows whether the AI is online. TB generation warns on unresolved warnings and blocks on errors. The ledger and TB can be exported to CSV.
- In `next.config.ts`, the rewrite destination comes from `API_URL` and `experimental.proxyTimeout` is set to 120000. The Next default of 30s would cut off a cold `/api/classify` call (40s measured). Per `frontend/AGENTS.md`, check `node_modules/next/dist/docs/` before using any Next API.

## Phases

Before coding each phase, I'll write its task-level plan with `superpowers:writing-plans`, then implement it test-first. I'll ask you once whether you want inline or subagent-driven execution. After each phase: tests green, `superpowers:verification-before-completion`, a code review, a commit on the feature branch, and a short checkpoint summary for you.

**Phase 0 — Setup**
- Create branch `feature/robust-ledger`.
- Commit your existing uncommitted Ollama migration **as its own commit** first.
- Save this plan as `docs/superpowers/specs/2026-09-28-ledgersync-robustness-design.md`.
- Pin `requirements.txt` to the `.venv` versions and add `xlrd`. Add `requirements-dev.txt` (pytest, httpx).
- Add a README (Python 3.11 venv, Ollama and model pull, run, test, eval, env vars).
- Gitignore `eval/private/`.

**Phase 1 — Reliability and security quick wins** (findings 4–7, 9–12, 16)
- Fix `/api/health`: real Ollama, model and OCR status.
- Turn analysis into a job (`jobs.py`): the endpoint validates and reads the upload, then returns `job_id`, and one worker thread does the work. This removes the event-loop blocking and gives progress and cancel.
- Create `ollama_client.py`, `intake.py`, `ocr.py` and `errors.py`. For now `qwen_service.py` uses them.
- Delete the regex fallback. Model failures return 503/504. Until Phase 4 adds the parser, CSV/Excel uploads also get a 503 while the AI is offline.
- Validate per row: invalid rows become warnings instead of failing the batch.
- Replace `print` with `logging`, and stop dumping OCR text.
- Bind to `127.0.0.1`, and enable reload only through an env var.
- Restrict CORS to `localhost:3000`.
- Move the legacy UI into `legacy/` and mount only that folder until Phase 5.
- Frontend: use relative URLs, set `proxyTimeout`, poll jobs (progress and Cancel), show readable errors, reset the file input, and make Manual Entry keep the user's amount and type (only the account comes from the model).
- Tests: TestClient with a fake LLM covering health, 413, 415, 422 (xls/encrypted/corrupt), 503/504, the job lifecycle (progress, cancel, expiry) and partial success.
- *Done when:* tests pass; `curl -i 127.0.0.1:8085/server.py` and `/.git/config` return 404; stopping Ollama shows a clear message in the UI.

**Phase 2 — Measure accuracy first**
- Fixtures:
  - `eval/make_fixtures.py` generates about 25 committed synthetic UK cases, each with an `expected.json` in the target schema.
  - Cases: receipts (text and rendered images, including cash/change, VAT number, phone number, mixed VAT); purchase and sales invoice PDFs; a scanned PDF; Barclays, HSBC, Lloyds, Monzo and Starling CSVs (preamble rows, cp1252 `£`, negatives in brackets); multi-sheet Excel; free text covering HMRC VAT, PAYE, a refund and a transfer; plus today's four probe cases.
  - Real anonymised documents go in the gitignored `eval/private/`.
- Harness:
  - `eval/run_eval.py` tests the HTTP API as a black box, with an adapter for the old response shape.
  - It reports row precision/recall, accuracy for amount, date, direction, account and VAT, whether the TB balances, and p50/p95 latency. Results go to `eval/results/*.json`.
  - Account and VAT are scored from Phase 3 onwards, once the API returns codes; they are "n/a" in the baseline.
- *Done when:* the baseline results are committed.

**Phase 3 — Accounting core (TDD, pure Python)** (findings 1, 3, 13)
- Build `accounts.py`, `money.py`, `models.py` and `posting.py`.
- Add endpoints `/api/accounts`, `/api/transactions/validate`, and v2 of `/api/trial-balance`.
- Switch the API to the new `Transaction` schema now, so the contract changes only once:
  - A temporary adapter maps `qwen_service` output (type → direction; account text → matching code, otherwise 9998 Suspense with an issue).
  - A minimal frontend update (types, code/name columns, money as strings) keeps the app working end to end until Phase 5.
- Tests:
  - Scenarios: expense+VAT, sale+VAT, supplier refund, customer refund, HMRC VAT payment, PAYE, drawings, loan, transfer, non-VAT-registered.
  - A seeded randomised test: 1,000 transactions, and the TB always balances.
  - Penny-rounding edge cases.
- *Done when:* tests pass, and the four probe cases produce a balanced TB. ("With sensible accounts" moved to Phase 4 on 2026-09-29: the Phase 3 eval balances all four, but the receipt, refund and Barclays accounts come from the Phase 1 prompt, which Phase 4 replaces.)

**Phase 4 — Hybrid extraction pipeline** (findings 2, 8, 10)
- Build `tabular.py`, `rules.py`, `pdf.py`, `llm_tasks.py`, `checks.py` and `pipeline.py`.
- Add `/api/classify`. Delete `qwen_service.py` and the Phase 3 adapter; the API contract stays unchanged.
- Tests: a fake LLM for units; opt-in live tests with `pytest -m llm`.
- *Done when:*
  - The eval shows **100% correct rows** (right amount, date and direction for every expected transaction) and 100% row precision on the CSV, Excel and text-PDF table fixtures. (Field accuracy alone can't gate this: it only covers rows that were found.)
  - Receipts and scans are measurably better than the baseline.
  - The four probe cases produce sensible accounts and directions: the Tesco receipt is one £12.50 expense, the supplier refund is money in, and the Barclays rows land on 7502, 4000 and 8205 (moved from Phase 3).
  - Every row has a method and an issues list.
  - With AI offline, CSV and Excel still import to Suspense.

**Phase 5 — Review UI and legacy retirement** (finding 14)
- Build the frontend described above.
- Port the useful legacy features: VAT fields, CSV export, row delete, sample data.
- Delete `index.html`, `app.js`, `styles.css`, the stale `implementation_plan.md`, `test.jpg`, the root `test_api.py` (replaced by `tests/`) and the `legacy/` mount.
- *Done when:* a browser run-through works end to end: upload a CSV, a receipt photo and a manual entry, edit a flagged row, and get a balanced TB. Offline mode works and there are no console errors.

**Phase 6 — Model and OCR tuning (driven by the eval)**
- Compare `qwen2.5vl:3b` with `qwen2.5vl:7b`. The 7B is a ~6 GB pull, and **I'll ask before downloading**.
- Compare OCR hint on and off, and EasyOCR with the already-pulled `PaddleOCR-VL`. Also tune image size and `num_ctx`.
- Choose defaults by accuracy and latency. Optional: learn vendor → account rules from UI corrections.

## Verification
- `pytest` (unit + API with a fake LLM) at every phase; `pytest -m llm` against the local Ollama.
- `python eval/run_eval.py`, compared against the committed baseline; each phase's accuracy claim cites these numbers.
- End to end: `python server.py` + `npm run dev`, driven in the browser pane.
- Security checks: 404 for source files and `.git`; the server is not reachable on the LAN IP.

## Out of scope for now
Auth and multi-user use, a database, bank feeds, MTD VAT submission, FX conversion (non-GBP rows are flagged instead), the flat-rate VAT scheme, HEIC photos.

## Risks and notes
- **Licences:**
  - To my knowledge, the Qwen2.5-VL-**3B** weights use the *Qwen Research* (non-commercial) licence, while 7B is Apache-2.0. Check the model card before commercial client use.
  - PyMuPDF is AGPL. That's fine for local use, but revisit before hosting (pypdfium2 + pdfplumber are permissive alternatives).
- Money serialised as strings is a small breaking API change, absorbed by Phase 5.
- Removing the legacy UI at `:8085/` happens only after its useful features are ported.
