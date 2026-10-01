# UK LedgerSync (prototype)

Turns UK financial inputs (receipt photos, bank statement CSV/Excel/PDF exports, pasted text
or manual entries) into categorised transactions and a double-entry trial balance. Documents are
read by Groq's hosted Qwen vision model (`qwen/qwen3.8-27b`): **uploaded documents are sent to
Groq.**

- `server.py` — FastAPI routes (port 8085, localhost only)
- `ledgersync/` — settings, typed errors, background jobs, the Groq client, upload checks, the
  extractor (prompt, schema, per-row validation), pipeline, chart of accounts, money and VAT,
  transaction checks and double-entry posting
- `frontend/` — Next.js UI (port 3000); calls the API through its `/api` rewrite

This prototype is being hardened; see
[the design spec](docs/superpowers/specs/2026-09-28-ledgersync-robustness-design.md) and
[the Groq model design](docs/superpowers/specs/2026-09-29-groq-vision-model-design.md).

## Prerequisites

- Python 3.11
- Node.js 20.9+
- A Groq API key ([console.groq.com/keys](https://console.groq.com/keys))

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                      # then paste your key after GROQ_API_KEY=
(cd frontend && npm install)
```

`.env` is git-ignored; never commit it or paste the key anywhere else.

## Run

```bash
.venv/bin/python server.py                # API on http://127.0.0.1:8085
(cd frontend && npm run dev)              # UI on http://localhost:3000
```

Analyses run as background jobs: `POST /api/analyze` returns `{job_id}`, then
`GET /api/jobs/{job_id}` reports progress and the result; `DELETE` cancels.
Without a key or an internet connection the API answers 503 with how to fix it; it never guesses.

The ledger endpoints use double entry: `GET /api/accounts` lists the chart (Sage 50-style
codes), `POST /api/transactions/validate` splits VAT and lists issues, and
`POST /api/trial-balance` takes `{"transactions": [...], "settings": {"vat_registered": true}}`
and always balances; rows with errors (unknown account, non-GBP, impossible VAT) get a 422.
Money is sent and returned as strings, e.g. `"12.50"`. Send `vat` only when the document shows
it; the API never changes it, and fills in `vat_posted` and `net` afresh on every call, so an
edited row can simply be sent back. `vat_posted` is the VAT booked: the amount shown; or, when a
person picked a rate in `vat_treatment`, the VAT inside the gross at that rate (flagged); otherwise
none, because VAT can only be reclaimed when it was charged.

## The AI model (Groq)

Pasted text, spreadsheets and PDFs with a text layer go to the model as text; photos and scanned
pages go as images (turned upright, at most 1600 px, one page per request). The model answers in a
strict JSON schema: one transaction per receipt or invoice, the direction of the money, an account
chosen from the chart, and the VAT printed on the document.

- Groq's free plan for this model allows about 30 requests and 8,000 tokens a minute and 200,000
  tokens a day (checked 2026-09-29); each image counts as 2,048 tokens. At the limit the app waits
  up to a minute, then reports a rate limit; `eval/run_eval.py --resume` runs those documents again.
- The daily allowance refills gradually, about 8,300 tokens an hour, so a full eval run leaves
  little for the rest of the day. Groq can also hold a request's expected output to 1,000 tokens a
  minute: on 2026-09-29 it refused a few small documents that way (its message suggests lowering
  `max_tokens`, i.e. `GROQ_MAX_OUTPUT_TOKENS`), yet let a 60-row statement's 3,500-token answer
  through minutes later. A refused document usually goes through when tried again later.
- `qwen/qwen3.8-27b` is a Groq *preview* model and may change; `GROQ_MODEL` switches it.
- `.venv/bin/python -m pytest -m llm` checks the key and model with one receipt photo.

## Configuration

Settings come from environment variables or `.env` (environment variables win).

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Your Groq key; put it in `.env` |
| `GROQ_MODEL` | `qwen/qwen3.8-27b` | Groq model; it must read images |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq's OpenAI-compatible API |
| `GROQ_TIMEOUT` | `60` | Seconds to wait for one model call |
| `GROQ_MAX_OUTPUT_TOKENS` | `4096` | Longest answer; raise it on a paid plan for very long statements |
| `GROQ_REASONING_EFFORT` | `none` | The model's "thinking" (`none`, `low`, …); off saves tokens |
| `GROQ_MAX_IMAGES` | `1` | Scanned pages per request; Groq allows 3, but 3 overflow the free plan's 8K tokens a minute |
| `LEDGERSYNC_BUSINESS_NAME` | — | Whose books these are; tells sales invoices from purchases |
| `LEDGERSYNC_HOST` / `LEDGERSYNC_PORT` | `127.0.0.1` / `8085` | API bind address |
| `LEDGERSYNC_RELOAD` | `0` | Auto-reload on code changes (development) |
| `LEDGERSYNC_WARMUP` | `1` | Check the key and model at startup, so the header shows the problem early |
| `LEDGERSYNC_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed browser origins |
| `LEDGERSYNC_MAX_UPLOAD_MB` | `20` | Largest accepted upload |
| `LEDGERSYNC_MAX_PDF_PAGES` | `30` | Most pages accepted in one PDF |
| `LEDGERSYNC_LOG_LEVEL` | `INFO` | Log level (document contents are never logged) |
| `API_URL` (frontend) | `http://127.0.0.1:8085` | Where the Next.js `/api` rewrite sends requests |

## Tests

```bash
.venv/bin/python -m pytest                # fast tests, no network
.venv/bin/python -m pytest -m llm         # live test against Groq (needs GROQ_API_KEY)
```

## Measuring accuracy

`eval/` holds 29 synthetic UK documents (receipts including a mixed-VAT one, invoices including
one already paid, bank statements in several bank formats including two of payees used nowhere
else, spreadsheets and pasted text), each with the transactions a bookkeeper would record
(`eval/fixtures/*/expected.json`), and a harness that runs them through a running API:

```bash
.venv/bin/python server.py                               # in one terminal
.venv/bin/python eval/run_eval.py --label my-change      # in another
```

The headline number is **correct rows**: the share of all expected transactions extracted with the
right amount, date and direction. The harness also reports row precision and recall, per-field
accuracy on the rows it found (amount, date, direction, account, VAT), how often the trial balance
balances, and latency. Results go to `eval/results/<date>-<label>.json` and are saved after every
document: `--resume` continues an interrupted run, `--resume --rerun --cases img-` runs selected
cases again, `--rescore` re-scores a saved report without the API, `--note` records the run
conditions, and `--pause 10` waits between documents to stay inside Groq's free-plan limits.

The committed `2026-09-28-baseline.json` and `2026-09-28-phase3.json` runs used the earlier local
model (`qwen2.5vl:3b` through Ollama on an 8 GB Mac), before the switch to Groq.

The runs on 2026-09-29 used Groq's `qwen/qwen3.8-27b` on the free plan (thinking off, photos sent
as images, `--pause 10`). The first, `groq-swap`, changed only the model and reached 95.1%
correct rows. The second, `groq-prompt`, added the new prompt and reached 100% correct rows with
90.2% account accuracy. Phase 3's local model had 41.5% and 33.9%.

The third, `accounts-general`, gave every account a plain definition and told the model to choose
by what was bought rather than by the shop. It sends a line that names only a payee who could be
selling anything, such as an online marketplace, to Suspense for a person to check. The fixtures
now label supermarkets as Refreshments and marketplaces as Suspense; on those labels `groq-prompt`
scores 83.7% on accounts (103 of 123). On the same 26 documents, account accuracy rose to 99.2%
(122 of 123), with correct rows and VAT still at 100%. The one miss is new: a refund from Amazon for
a returned printer went to Suspense, not Office Equipment. On a statement of 20 payees used nowhere
else (`csv-unseen-payees`), the model placed 18 accounts correctly before the change
(`unseen-before`) and all 20 after (`unseen-after`, and again in `accounts-general`).

Two rewordings of the account rule were then measured and dropped, so the rule stays as in
`accounts-general`, whose one miss at least lands in Suspense, where a person checks it:
- Letting a named item decide whoever the seller is (`accounts-precedence`) fixed the refund, but
  twice out of two it sent Waitrose to Subsistence and Google Ads, which has no account in the
  chart, to Subscriptions and Software instead of Suspense: 98.6% (141 of 143).
- Spelling out an order, what was bought and then the account that fits (`accounts-order`), sent
  eight Tesco lines, an accountant's invoice and the refund itself to Suspense.

A second held-out statement (`csv-unseen-mixed`) was written before that last attempt, since the
first had by then been used to choose between wordings. It names items bought from sellers of
almost anything and has costs no account fits. Every wording placed 18 or 19 of its 20 accounts,
with all five named items and both sellers of anything right. A council penalty charge went to
Travel every time, and LinkedIn Ads went to Subscriptions and Software in one of two runs of the
rule kept (`unseen-mixed`). The chart has no account for either cost (fines, advertising), which is
also why Google Ads goes astray.

To measure real documents, put anonymised copies in `eval/private/<case>/` (the input file plus an
`expected.json` in the same format) and add `--private`; that folder is never committed. After
editing `eval/eval_cases.py`, regenerate the fixtures with `.venv/bin/python eval/make_fixtures.py`.
