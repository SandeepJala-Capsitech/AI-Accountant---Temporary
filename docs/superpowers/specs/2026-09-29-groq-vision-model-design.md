# Groq Vision Model for the MVP Demo — Design

**Date:** 2026-09-29 · **Status:** for review · **Builds on:** `2026-09-28-ledgersync-robustness-design.md` (after Phase 3)

> **Update 2026-09-29 (user decision): Groq only.** The local Ollama model and OCR were removed, so decision 7 no longer applies. Settings now use Groq's own names: `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_BASE_URL`, `GROQ_TIMEOUT`, `GROQ_MAX_OUTPUT_TOKENS`, `GROQ_REASONING_EFFORT` and `GROQ_MAX_IMAGES`. Code names changed too:
>
> - `GroqClient` in `ledgersync/groq_client.py` replaces `OpenAICompatibleClient`;
> - `TransactionExtractor` in `ledgersync/extractor.py` replaces `qwen_service.py`;
> - `create_app(model_client=…)` replaces `ollama=`.
>
> After the final review, `GROQ_MAX_IMAGES` defaults to 1 page per request. Three pages come to about 7.2K tokens before any answer, which overflows the free plan's 8K tokens a minute; paid plans can set 3.
>
> Below, `LEDGERSYNC_AI_*` settings and the provider switch are the original design.

## Goal

For the MVP demo, read documents with a stronger hosted vision model, **`qwen/qwen3.8-27b` on Groq**, instead of the local `qwen2.5vl:3b`. Also fix the prompt problems the eval exposed: receipts split into item lines, the refund booked as money out, and accounts guessed from free text.

**Success:**
- The eval, run on Groq, beats the Phase 3 run: correct rows above 0.415, image correct rows above 0.0, account accuracy above 0.339.
- The demo works end to end in the UI.

## Decisions and assumptions

| | Decision | Source |
|---|---|---|
| 1 | Groq, through its OpenAI-compatible API (`https://api.groq.com/openai/v1`); the user has a key | user |
| 2 | MVP demo: data-protection terms are out of scope. The README says documents go to the provider when it is switched on. | user |
| 3 | Swap the model **and** improve the prompt, measuring the eval after each step | user |
| 4 | Model `qwen/qwen3.8-27b`: 27B parameters, 131K context, up to 3 images per request, each image counts as 2,048 input tokens, strict JSON-schema output. It is Groq's only model that reads images, and it is in **preview**, so it may change. (`openai/gpt-oss-20b` is text-only.) | Groq docs, checked 2026-09-29 |
| 5 | Groq's free plan for this model: 30 requests/min, 1,000/day, 8K tokens/min, 200K tokens/day; over the limit gives HTTP 429. Paid is about $0.80 per million input and $4 per million output tokens. | Groq docs, checked 2026-09-29 |
| 6 | Thinking is off (`reasoning_effort: "none"`): the model is a reasoning model, and thinking tokens would eat the free 8K tokens/min. A setting can turn it on. | assumption |
| 7 | Ollama stays the default provider in code, so tests and anyone without a key keep working; `.env` switches the demo to Groq | assumption |

## Part 1 — Components (approved 2026-09-29)

1. **`ledgersync/openai_client.py`**: an `OpenAICompatibleClient` for any OpenAI-compatible API.
   - It has the same five members as `OllamaClient` (`model`, `health`, `ensure_available`, `chat_json`, `warm_up`), so the extractor and server need no changes to call it.
   - It uses the standard library `urllib`, like `OllamaClient`, so there is no new dependency.
   - `chat_json` posts to `/chat/completions` with:
     - `model` and `messages`, with images as `image_url` parts holding `data:image/jpeg;base64,…` URLs;
     - `response_format: {"type": "json_schema", "json_schema": {"name": "transactions", "schema": <strict schema>, "strict": true}}`;
     - `temperature: 0`, `max_completion_tokens`, and `reasoning_effort` when set.
   - A `strict_schema()` helper turns the Pydantic schema into what strict mode accepts: `$defs` inlined, `additionalProperties: false`, every property required, and only supported keywords kept.
2. **Settings.**
   - `LEDGERSYNC_AI_PROVIDER`: `ollama` (default) or `openai`.
   - `LEDGERSYNC_AI_BASE_URL`, `LEDGERSYNC_AI_MODEL` and `LEDGERSYNC_AI_API_KEY`.
   - `LEDGERSYNC_AI_TIMEOUT`: 60 s.
   - `LEDGERSYNC_AI_MAX_OUTPUT_TOKENS`: 4096. Groq does not document whether the requested maximum counts toward the free 8K tokens/min, so 4096 keeps a one-photo request under it either way; a 60-row statement's answer is about 3.5K tokens.
   - `LEDGERSYNC_AI_REASONING_EFFORT`: `none`.
   - `LEDGERSYNC_AI_MAX_IMAGES`: 3.
   - `LEDGERSYNC_BUSINESS_NAME`: optional.

   A small loader reads a `.env` file at the project root, which is already git-ignored; real environment variables win. A committed `.env.example` shows the Groq values with an empty key. The key is sent only in the `Authorization` header to the configured base URL. It never appears in logs, `/api/health`, error messages or `repr(Settings)`.
3. **Routing when the API provider is on.**
   - Photos and scanned PDF pages go to the model **as images**, never through OCR, so EasyOCR and its roughly 2 GB never load. Before sending, each image is rotated upright using the photo's orientation data (EXIF), downscaled to at most 1600 px on the long side, and re-encoded as JPEG (quality 85).
   - Scans are sent 3 pages per request; the rows and warnings of all requests are merged, and warnings name their pages.
   - Pasted text, CSV/Excel and text PDFs go as text, as today.
   - The client says which route it wants with `prefers_images` (Ollama: `False`, so OCR first, as today) and `max_images` (Ollama: no limit).

## Part 2 — Prompt and data flow

- **Messages.** A system message holds the instructions. A user message holds the document: text inside `<document>…</document>` tags, or "The document is attached as N image(s)" with the images.
- **Instructions:**
  1. *Perspective:* "You keep the books of {business name, or "the business"}." `in` means money into its bank account: sales, refunds received, loans, capital. `out` means money paid out: purchases, expenses, taxes, wages, refunds given, drawings, transfers to savings.
  2. *Receipts and invoices:* **one** transaction, for the total paid or charged including VAT. Item lines, subtotals, cash tendered, change and card lines are not transactions.
  3. *Statements and spreadsheets:* one transaction per movement. The amount is the money that moved; the direction comes from the paid-in/paid-out columns or the sign. Balance and total lines are not transactions.
  4. *Account:* pick from the chart of accounts. The prompt lists each code and name with a few examples, e.g. "7502 Telephone and Internet (phone, broadband, mobile)". Use 9998 Suspense when unsure.
  5. *VAT:* the VAT amount printed on the document, or `null`. Never calculate it; the ledger does that.
  6. *Dates:* UK day-first, as `YYYY-MM-DD`, or `null`.
- **Schema (strict).** `{"transactions": [{description, date, amount, direction, account, vat, currency}]}`:
  - `direction` is an enum of `in` and `out`;
  - `account` is an enum of `"<code> <name>"` strings for every chart account except 1200 Bank and the VAT control accounts 2200/2201, since the ledger splits VAT itself;
  - `date` and `vat` may be null.

  Amounts are checked in Python, not in the schema.
- **Adapter.** `to_transactions` switches to the new row shape:
  - the account code is the first four characters of `account` (a code not in the chart goes to Suspense with a warning);
  - `direction` comes from the field, and a negative amount still means money out;
  - `vat` goes to `Transaction.vat`, the VAT shown, so `vat_posted` uses the document's VAT and estimates only when it is `null`.

  The free-text name matching (`match_name`, `account_guessed`) is then unused and is removed with its tests.
- **One prompt for both providers.** The local model gets the same prompt and schema. Ollama enforces the enum too, and the chart list adds about 600 tokens, which fits its 4,096-token context.

## Part 3 — Errors, tests, measurement

**Errors.** Each outcome maps to the existing typed errors, so the UI and jobs need no changes. One new code, `ai_rate_limited` (503), is added and counts as a harness error, so the eval's `--resume` re-runs it.

| Provider outcome | Result |
|---|---|
| No API key set | 503 `ai_offline`: "Set LEDGERSYNC_AI_API_KEY in .env" |
| Cannot connect (after one retry) | 503 `ai_offline` |
| 401 / 403 | 503 `ai_offline`: "the AI provider rejected the API key" |
| 404 | 503 `ai_offline`: "model … is not available from the provider" |
| 413, or 400 about context length | 502 `ai_input_too_long`: too large for the provider's limit on this plan |
| 429 | Wait for `retry-after` (10 s if absent) and retry, up to 60 s in total per call; then 503 `ai_rate_limited` |
| 5xx (after one retry), other 4xx | 502 `ai_error`, with the provider's `error.message` (never the key) |
| Timeout | 504 `ai_timeout` |
| `finish_reason: "length"` | 502 `ai_output_truncated` |
| Empty or non-JSON answer | 502 `ai_output_invalid` |

**Health.** The client calls `GET /models` with the key: the model must be listed and active. The result is cached for 5 minutes, not 15 s, so the UI's 30-second health polling does not spend the free plan's daily requests. `warm_up` only runs that check.

**Tests** (TDD; no network except the opt-in live test):
- **Client, with a fake HTTP opener:**
  - request shape: URL, auth header, image parts, strict schema, `temperature`, `max_completion_tokens`, `reasoning_effort`;
  - every row of the error table, including 429 waits followed by success;
  - the key never appears in errors or logs.
- **`strict_schema()`:** refs inlined, `additionalProperties: false`, all fields required, unsupported keywords dropped.
- **Settings:** provider selection; `.env` loading, with the environment winning; a blank key means "not configured"; the key is not in `repr`.
- **Pipeline:**
  - with an image-preferring client, OCR is never called, the image arrives upright, at most 1600 px and as JPEG, and a 5-page scan makes 2 calls (3 pages, then 2);
  - text inputs and the Ollama route are unchanged.
- **Prompt:** the account enum equals the chart minus 1200, 2200 and 2201; the business name appears when set; the system and user messages are separate.
- **Adapter:** the new row shape, with the account code, VAT shown and direction; the existing API and eval tests move to the new shape.
- **Live smoke test** (`-m llm`, skipped without a key): one receipt image through Groq returns one transaction.

**Measurement:**
1. **Swap only** (old prompt): eval label `groq-swap`.
2. **New prompt:** eval label `groq-prompt`, with `LEDGERSYNC_BUSINESS_NAME="Northbridge Consulting Ltd"` (the business the fixtures belong to).

Both runs go on the free plan, with the provider, model, plan and reasoning setting recorded in `--note`, and both are compared with `2026-09-28-phase3.json`. The 60-row statement may exceed the free plan's 8K tokens/min in one request. It would then be recorded as `ai_input_too_long`, a plan limit rather than a model failure, and noted.

**Done when:**
- tests pass;
- both eval runs are committed;
- `groq-prompt` beats Phase 3 on correct rows and account accuracy;
- a browser demo on Groq books a pasted line, a receipt photo and a manual entry into a balanced trial balance.

## Out of scope for the MVP

- A data-protection review, and a paid plan.
- Comparing several models.
- Phase 4's bank parsers, payee rules, PDF tables and amount cross-checks.
- Business settings sent with each request (the business name is an environment setting for now).
- Streaming.

## Effect on the roadmap

- **Phase 4:** its prompt work (perspective, receipt rules, chart enum) is done here. The deterministic parsers stay, because they give exact amounts and work offline. The "sensible accounts" probe condition moved there may already be met by this work; the `groq-prompt` run will show.
- **Phase 6:** for the API route, "OCR on or off" is answered: off. Local model tuning matters only for offline use.
