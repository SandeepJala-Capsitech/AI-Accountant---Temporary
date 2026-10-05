# Phase 2 — Measure Accuracy First: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable accuracy measurement. It has three parts:
- 25 synthetic UK documents, each with the transactions a bookkeeper would record from it.
- A black-box harness that runs them through the live API and scores the output.
- A committed baseline for later phases to beat.

**Architecture:** `eval/` is self-contained dev tooling and imports nothing from the app, so it can measure any version of the API.
- `eval_cases.py` holds the ground truth. Each case's input file and `expected.json` are generated from one definition, so they can't disagree.
- `eval_scoring.py` has pure functions that adapt any API result shape, pair predicted rows with expected rows, and compute metrics.
- `run_eval.py` drives `POST /api/analyze` and the job API over HTTP, then writes `eval/results/<date>-<label>.json`.

**Tech Stack:** Python 3.11, httpx2 (dev dependency, already installed), Pillow and PyMuPDF to render receipts and PDFs, openpyxl, pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-ledgersync-robustness-design.md` (Phase 2)

## Global Constraints

- The directory is `eval/`, but it is **not** a Python package: `import eval` would shadow the builtin. Its modules are imported by bare name (`eval_scoring`, `eval_cases`, `run_eval`), with `eval` on the pytest `pythonpath`.
- The harness talks to the API only over HTTP (default `http://127.0.0.1:8085`).
- Expected rows use the target schema: `date` (ISO or null), `direction` (`in`/`out`), `gross` (a 2-decimal string), `vat` (2-decimal string, or null when the document shows no VAT), `account_code` (a provisional Sage 50 nominal code) and `description`.
- Account and VAT are scored only when the API returns them. In the baseline they are "n/a".
- Real client documents live only in the git-ignored `eval/private/`. Their results go to `eval/private/results/`, never to `eval/results/`.
- Money is compared as `Decimal` rounded to pennies. Floats are never compared.

## Review Focus

1. **API not running when the harness starts.** Print how to start it, exit non-zero, and write no results file. Test: `test_unreachable_api_exits_with_advice` (Task 3).
2. **A case fails (AI offline or timeout).** Record its error code with zero recall and keep going through the other cases. Test: `test_failed_case_is_recorded_not_fatal` (Task 3).
3. **The model repeats a row.** Each expected row pairs at most once, so duplicates count against precision. Test: `test_each_expected_row_pairs_with_at_most_one_prediction` (Task 1).
4. **Private documents must not reach committed results.** `--private` writes to a git-ignored folder. Test: `test_private_results_are_git_ignored` (Task 3).
5. **The model returns UK-format dates** ("03/09/2026"). These are normalised, not counted wrong. Test: `test_dates_are_normalised_day_first` (Task 1).

---

### Task 1: Scoring functions

**Files:**
- Modify: `pytest.ini` (add `eval` to `pythonpath`)
- Create: `eval/eval_scoring.py`
- Test: `tests/test_eval_scoring.py`

**Interfaces:**
- Produces (used by Task 3):
  - `to_money(value) -> Optional[Decimal]`
  - `normalise_date(value) -> Optional[str]`
  - `expected_rows(rows: list[dict]) -> list[dict]`
  - `adapt_rows(result: dict) -> list[dict]`
  - `match_rows(expected, predicted) -> list[tuple[int, int]]`
  - `score_case(expected, predicted) -> dict` with keys `expected`, `predicted`, `matched`, `checks` (`{field: [correct, scored]}` for amount/date/direction/account/vat) and `exact`
  - `summarise(cases: list[dict]) -> dict`
- Row dicts carry `date`, `direction`, `gross` (Decimal), `vat` (Decimal or None), `account_code`, `description`, `has_account` and `has_vat`.

- [ ] **Step 1: `pytest.ini`**: change `pythonpath = .` to `pythonpath = . eval`.

- [ ] **Step 2: Write the failing tests** — `tests/test_eval_scoring.py`:

```python
from decimal import Decimal

import pytest

from eval_scoring import adapt_rows, expected_rows, match_rows, normalise_date, score_case, summarise

EXPECTED = expected_rows([{"date": "2026-09-03", "direction": "out", "gross": "12.50", "vat": None,
                           "account_code": "7406", "description": "Tesco Express"}])


def phase1_row(amount, type_="expense", date="2026-09-03", description="x", account="General"):
    return {"description": description, "date": date, "amount": amount, "currency": "GBP",
            "type": type_, "account": account}


def test_phase1_results_are_adapted_to_direction_and_gross():
    rows = adapt_rows({"data": [phase1_row(72.0), phase1_row(3600, "revenue", date="02/09/2026")]})
    assert [(r["direction"], r["gross"], r["date"]) for r in rows] == [
        ("out", Decimal("72.00"), "2026-09-03"), ("in", Decimal("3600.00"), "2026-09-02")]
    assert rows[0]["has_account"] is False and rows[0]["has_vat"] is False


def test_later_results_with_codes_are_used_as_is():
    rows = adapt_rows({"transactions": [{"date": "2026-09-03", "direction": "out", "gross": "12.50",
                                         "vat": "2.08", "account_code": "7406", "description": "Tesco"}]})
    assert (rows[0]["gross"], rows[0]["vat"], rows[0]["account_code"]) == (Decimal("12.50"), Decimal("2.08"), "7406")
    assert rows[0]["has_account"] and rows[0]["has_vat"]


@pytest.mark.parametrize("raw, iso", [
    ("2026-09-03", "2026-09-03"), ("03/09/2026", "2026-09-03"), ("2026-09-03 00:00:00", "2026-09-03"),
    ("3 Sept", None), (None, None),
])
def test_dates_are_normalised_day_first(raw, iso):
    assert normalise_date(raw) == iso


def test_receipt_over_extraction_is_scored_honestly():
    # Phase 1 probe: one £12.50 purchase came back as six rows (items and TOTAL as revenue,
    # CASH and CHANGE as expenses).
    predicted = adapt_rows({"data": [phase1_row(3.5, "revenue"), phase1_row(2.8, "revenue"),
                                     phase1_row(6.2, "revenue"), phase1_row(12.5, "revenue", description="TOTAL"),
                                     phase1_row(20.0), phase1_row(7.5)]})
    result = score_case(EXPECTED, predicted)
    assert (result["expected"], result["predicted"], result["matched"]) == (1, 6, 1)
    assert result["checks"]["amount"] == [1, 1]
    assert result["checks"]["direction"] == [0, 1]
    assert result["checks"]["account"] == [0, 0]      # not scored: Phase 1 returns no account codes
    assert result["exact"] is False


def test_wrong_amount_still_pairs_on_date_and_description():
    result = score_case(EXPECTED, adapt_rows({"data": [phase1_row(125.0, description="Tesco Express store")]}))
    assert result["matched"] == 1 and result["checks"]["amount"] == [0, 1]


def test_unrelated_rows_do_not_pair():
    predicted = adapt_rows({"data": [phase1_row(99.0, date="2026-10-01", description="Other")]})
    assert score_case(EXPECTED, predicted)["matched"] == 0


def test_perfect_extraction_is_exact():
    predicted = adapt_rows({"transactions": [{"date": "2026-09-03", "direction": "out", "gross": "12.50",
                                              "vat": None, "account_code": "7406", "description": "Tesco"}]})
    result = score_case(EXPECTED, predicted)
    assert result["exact"] is True and result["checks"]["account"] == [1, 1]


def test_each_expected_row_pairs_with_at_most_one_prediction():
    row = {"date": "2026-09-01", "direction": "out", "gross": "72.00", "vat": None,
           "account_code": "7502", "description": "BT"}
    predicted = adapt_rows({"data": [phase1_row(72.0, date="2026-09-01", description="BT")] * 2})
    assert match_rows(expected_rows([row]), predicted) == [(0, 0)]
    assert score_case(expected_rows([row]), predicted)["exact"] is False


def test_summary_aggregates_rows_fields_latency_and_errors():
    none = {"amount": [0, 0], "date": [0, 0], "direction": [0, 0], "account": [0, 0], "vat": [0, 0]}
    cases = [
        {"kind": "text", "expected": 1, "predicted": 6, "matched": 1, "exact": False, "latency_s": 10.0,
         "error": None, "tb_balanced": False,
         "checks": {**none, "amount": [1, 1], "date": [1, 1], "direction": [0, 1]}},
        {"kind": "table", "expected": 3, "predicted": 0, "matched": 0, "exact": False, "latency_s": 30.0,
         "error": "ai_offline", "tb_balanced": None, "checks": none},
    ]
    s = summarise(cases)
    assert s["row_precision"] == round(1 / 6, 3) and s["row_recall"] == 0.25
    assert s["field_accuracy"] == {"amount": 1.0, "date": 1.0, "direction": 0.0, "account": None, "vat": None}
    assert (s["exact_cases"], s["tb_balanced"]) == (0.0, 0.0)
    assert (s["latency_p50_s"], s["latency_p95_s"]) == (10.0, 30.0)
    assert s["errors"] == {"ai_offline": 1}
```

- [ ] **Step 3: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_eval_scoring.py -q`. Expected: `ModuleNotFoundError: No module named 'eval_scoring'`.

- [ ] **Step 4: Implement** — `eval/eval_scoring.py`:

```python
"""Scoring for the accuracy harness: turns any API result into comparable rows, pairs them with
the expected rows and computes metrics. Pure functions, no I/O."""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

FIELDS = ("amount", "date", "direction", "account", "vat")
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y")


def to_money(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace("£", "").replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def normalise_date(value) -> Optional[str]:
    """ISO date from ISO or UK day-first text; None when absent or unreadable."""
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text[:10]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def expected_rows(rows: list[dict]) -> list[dict]:
    return [{**row, "gross": to_money(row["gross"]), "vat": to_money(row.get("vat"))} for row in rows]


def adapt_rows(result: dict) -> list[dict]:
    """Phase 1 results: {"data": [{"type", "amount", "date", "account"}]}.
    Later phases: {"transactions": [{"direction", "gross", "vat", "account_code", "date"}]}."""
    rows = result.get("transactions")
    if rows is None:
        rows = result.get("data") or []
    adapted = []
    for row in rows:
        direction = row.get("direction")
        if direction is None and row.get("type") in ("expense", "revenue"):
            direction = "out" if row["type"] == "expense" else "in"
        adapted.append({
            "date": normalise_date(row.get("date")),
            "direction": direction,
            "gross": to_money(row["gross"] if "gross" in row else row.get("amount")),
            "vat": to_money(row.get("vat")),
            "account_code": row.get("account_code"),
            "description": str(row.get("description") or ""),
            "has_account": "account_code" in row,
            "has_vat": "vat" in row,
        })
    return adapted


def _overlap(a: dict, b: dict) -> float:
    ta, tb = (set(re.findall(r"[a-z0-9]{3,}", r.get("description", "").lower())) for r in (a, b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _same_amount(e: dict, p: dict) -> bool:
    return p["gross"] is not None and p["gross"] == e["gross"]


def _same_date(e: dict, p: dict) -> bool:
    return p["date"] is not None and p["date"] == e["date"]


def match_rows(expected: list[dict], predicted: list[dict]) -> list[tuple[int, int]]:
    """Greedy one-to-one pairing, best pairs first. A pair must share the amount, or the date
    plus a similar description, to count as the same transaction."""
    candidates = []
    for i, e in enumerate(expected):
        for j, p in enumerate(predicted):
            if _same_amount(e, p) or (_same_date(e, p) and _overlap(e, p) >= 0.3):
                score = (4 * _same_amount(e, p) + 2 * _same_date(e, p)
                         + (p["direction"] == e["direction"]) + _overlap(e, p))
                candidates.append((-score, i, j))
    used_e, used_p, pairs = set(), set(), []
    for _, i, j in sorted(candidates):
        if i not in used_e and j not in used_p:
            used_e.add(i)
            used_p.add(j)
            pairs.append((i, j))
    return sorted(pairs)


def score_case(expected: list[dict], predicted: list[dict]) -> dict:
    pairs = match_rows(expected, predicted)
    checks = {field: [0, 0] for field in FIELDS}

    def check(field: str, ok: bool) -> None:
        checks[field][0] += bool(ok)
        checks[field][1] += 1

    for i, j in pairs:
        e, p = expected[i], predicted[j]
        check("amount", _same_amount(e, p))
        check("date", p["date"] == e["date"])
        check("direction", p["direction"] == e["direction"])
        if e.get("account_code") and p["has_account"]:
            check("account", p["account_code"] == e["account_code"])
        if e.get("vat") is not None and p["has_vat"]:
            check("vat", p["vat"] == e["vat"])
    exact = (len(pairs) == len(expected) == len(predicted)
             and all(correct == scored for correct, scored in checks.values()))
    return {"expected": len(expected), "predicted": len(predicted), "matched": len(pairs),
            "checks": checks, "exact": exact}


def _ratio(num: float, den: float) -> Optional[float]:
    return round(num / den, 3) if den else None


def _percentile(values: list[float], pct: int) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def summarise(cases: list[dict]) -> dict:
    totals = {key: sum(c[key] for c in cases) for key in ("expected", "predicted", "matched")}
    tb = [c["tb_balanced"] for c in cases if c.get("tb_balanced") is not None]
    latencies = [c["latency_s"] for c in cases if c.get("latency_s") is not None]
    return {
        "cases": len(cases),
        **totals,
        "row_precision": _ratio(totals["matched"], totals["predicted"]),
        "row_recall": _ratio(totals["matched"], totals["expected"]),
        "field_accuracy": {f: _ratio(sum(c["checks"][f][0] for c in cases), sum(c["checks"][f][1] for c in cases))
                           for f in FIELDS},
        "exact_cases": _ratio(sum(1 for c in cases if c["exact"]), len(cases)),
        "tb_balanced": _ratio(sum(tb), len(tb)),
        "latency_p50_s": _percentile(latencies, 50),
        "latency_p95_s": _percentile(latencies, 95),
        "errors": dict(Counter(c["error"] for c in cases if c.get("error"))),
    }
```

- [ ] **Step 5: Run and confirm pass.** Run `.venv/bin/python -m pytest tests/test_eval_scoring.py -q`. Expected: 13 passed.

- [ ] **Step 6: Commit**

```bash
git add pytest.ini eval/eval_scoring.py tests/test_eval_scoring.py
git commit -m "Add accuracy scoring: adapt API results, pair rows, per-field metrics"
```

---

### Task 2: Ground-truth cases and fixture generator

**Files:**
- Create: `eval/eval_cases.py`, `eval/make_fixtures.py`, and `eval/fixtures/<case>/{input.*, expected.json}` (generated, committed)
- Test: `tests/test_eval_fixtures.py`

**Interfaces:**
- Produces:
  - `eval_cases.CASES: tuple[Case, ...]`
  - `Case(id, kind, filename, render, transactions)`
  - `Tx(date, description, amount, account, vat=None)` with `.expected`
  - `make_fixtures.write_all(target=FIXTURES) -> list[Path]`
  - Each case's `expected.json` is `{"case", "kind", "input", "rows": [...]}` (consumed by Task 3's `load_cases`).

- [ ] **Step 1: Write the failing tests** — `tests/test_eval_fixtures.py`:

```python
import json
import re
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from ledgersync.intake import from_text, load_upload

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"
CASE_DIRS = sorted(p for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.exists() else []
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def spec_of(case_dir):
    return json.loads((case_dir / "expected.json").read_text())


def intake_of(case_dir, spec):
    data = (case_dir / spec["input"]).read_bytes()
    if spec["input"].endswith(".txt"):
        return from_text(data.decode("utf-8"), 10**7)
    return load_upload(spec["input"], data, max_pdf_pages=30)


def test_there_are_25_cases_covering_every_input_kind():
    assert len(CASE_DIRS) == 25
    assert {spec_of(d)["kind"] for d in CASE_DIRS} == {"text", "table", "pdf", "image"}


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_fixture_is_readable_and_well_formed(case_dir):
    spec = spec_of(case_dir)
    assert intake_of(case_dir, spec).kind == spec["kind"]
    assert spec["rows"], "every case records at least one transaction"
    for row in spec["rows"]:
        assert row["direction"] in ("in", "out")
        assert Decimal(row["gross"]) > 0 and Decimal(row["gross"]).as_tuple().exponent == -2
        assert row["vat"] is None or Decimal(row["vat"]) < Decimal(row["gross"])
        assert re.fullmatch(r"\d{4}", row["account_code"])
        assert re.fullmatch(r"2026-\d\d-\d\d", row["date"])


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_every_expected_amount_is_printed_in_the_document(case_dir):
    # Independent check on the generator: the answer must actually be in the document.
    spec = spec_of(case_dir)
    if spec["kind"] == "image":
        pytest.skip("pixels only")
    if spec["kind"] == "pdf":
        with pymupdf.open(case_dir / spec["input"]) as doc:
            text = "".join(page.get_text() for page in doc)
        if not text.strip():
            pytest.skip("scanned PDF has no text layer")
    else:
        text = intake_of(case_dir, spec).text
    printed = {Decimal(n.replace(",", "")) for n in NUMBER.findall(text)}
    for row in spec["rows"]:
        assert Decimal(row["gross"]) in printed, row
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_eval_fixtures.py -q`. Expected: `test_there_are_25_cases_covering_every_input_kind` FAILS (`assert 0 == 25`). The parametrised tests collect nothing.

- [ ] **Step 3: Implement** — `eval/eval_cases.py`:

```python
"""Ground truth for the accuracy fixtures: 25 synthetic UK documents and the transactions a
bookkeeper would record from each, from the point of view of Northbridge Consulting Ltd.

Account codes are provisional Sage 50 UK nominal codes. Phase 3's chart of accounts must
contain every code used here, or this file and the fixtures must be regenerated."""
from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, Optional

BUSINESS = "Northbridge Consulting Ltd"


@dataclass(frozen=True)
class Tx:
    date: str                   # ISO date
    description: str
    amount: str                 # signed for the business: "-72.00" is money out
    account: str                # provisional Sage 50 nominal code
    vat: Optional[str] = None   # only when the document itself shows the VAT

    @property
    def out(self) -> bool:
        return self.amount.startswith("-")

    @property
    def value(self) -> str:
        return self.amount.lstrip("-")

    @property
    def expected(self) -> dict:
        return {"date": self.date, "direction": "out" if self.out else "in", "gross": self.value,
                "vat": self.vat, "account_code": self.account, "description": self.description}


@dataclass(frozen=True)
class Case:
    id: str
    kind: str                   # text | table | pdf | image
    filename: str
    render: Callable[[], bytes]
    transactions: tuple[Tx, ...]


# ── Renderers ───────────────────────────────────────────────────────────────────

def _uk(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def _text(body: str) -> Callable[[], bytes]:
    return lambda: body.strip().encode("utf-8") + b"\n"


def _csv(rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode(encoding)


def _receipt_png(lines: list[str], rotate: float = 0.0, noise: bool = False) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=22)
    img = Image.new("L", (600, 40 + 32 * len(lines)), 255)
    draw = ImageDraw.Draw(img)
    for n, line in enumerate(lines):
        draw.text((28, 20 + 32 * n), line, fill=0, font=font)
    if noise:
        rng = random.Random(7)
        pixels = img.load()
        for _ in range(img.width * img.height // 40):
            pixels[rng.randrange(img.width), rng.randrange(img.height)] = rng.randrange(150, 256)
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor=255, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _text_pdf(lines: list[str]) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    for n, line in enumerate(lines):
        page.insert_text((48, 64 + 15 * n), line, fontname="cour", fontsize=9)
    return doc.tobytes()


def _scanned_pdf(lines: list[str]) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_receipt_png(lines, rotate=0.8, noise=True))
    return doc.tobytes()


def _statement_csv(txs: tuple[Tx, ...], opening: str) -> bytes:
    rows, balance = [["Date", "Description", "Amount", "Balance"]], Decimal(opening)
    for tx in txs:
        balance += Decimal(tx.amount)
        rows.append([_uk(tx.date), tx.description, tx.amount, f"{balance:.2f}"])
    return _csv(rows)


def _barclays_export(entries) -> bytes:
    rows = [["Number", "Date", "Account", "Amount", "Subcategory", "Memo"]]
    rows += [["", _uk(tx.date), "20-32-06 13576543", tx.amount, sub, tx.description] for tx, sub in entries]
    return _csv(rows)


def _hsbc(txs: tuple[Tx, ...]) -> bytes:        # HSBC exports have no header row
    return _csv([[_uk(tx.date), tx.description, tx.amount] for tx in txs])


def _lloyds(entries, opening: str) -> bytes:
    rows = [["Transaction Date", "Transaction Type", "Sort Code", "Account Number", "Transaction Description",
             "Debit Amount", "Credit Amount", "Balance"]]
    balance = Decimal(opening)
    for tx, type_ in entries:
        balance += Decimal(tx.amount)
        rows.append([_uk(tx.date), type_, "'30-94-57", "12345678", tx.description,
                     tx.value if tx.out else "", "" if tx.out else tx.value, f"{balance:.2f}"])
    return _csv(rows)


def _monzo(entries) -> bytes:
    rows = [["Transaction ID", "Date", "Time", "Type", "Name", "Emoji", "Category", "Amount", "Currency",
             "Local amount", "Local currency", "Notes and #tags", "Address", "Receipt", "Description",
             "Category split"]]
    for n, (tx, time_, type_, name, category) in enumerate(entries, start=1):
        rows.append([f"tx_0000A{n}", _uk(tx.date), time_, type_, name, "", category, tx.amount, "GBP",
                     tx.amount, "GBP", "", "", "", tx.description, ""])
    return _csv(rows)


def _starling(entries, opening: str) -> bytes:
    rows = [["Date", "Counter Party", "Reference", "Type", "Amount (GBP)", "Balance (GBP)", "Spending Category", "Notes"],
            ["01/09/2026", "Opening Balance", "", "", "0.00", f"{Decimal(opening):.2f}", "", ""]]
    balance = Decimal(opening)
    for tx, party, reference, type_, category in entries:
        balance += Decimal(tx.amount)
        rows.append([_uk(tx.date), party, reference, type_, tx.amount, f"{balance:.2f}", category, ""])
    return _csv(rows)


def _preamble_cp1252(txs: tuple[Tx, ...], opening: str) -> bytes:
    rows = [["Account Name:", BUSINESS], ["Account Number:", "43018822"],
            ["Statement Period:", "01/09/2026 - 30/09/2026"], [], ["Date", "Details", "Amount", "Balance"]]
    balance = Decimal(opening)
    for tx in txs:
        balance += Decimal(tx.amount)
        value = Decimal(tx.value)
        rows.append([_uk(tx.date), tx.description, f"(£{value:,.2f})" if tx.out else f"£{value:,.2f}",
                     f"£{balance:,.2f}"])
    return _csv(rows, encoding="cp1252")


def _utf16_tsv(txs: tuple[Tx, ...]) -> bytes:  # Excel "Unicode Text": UTF-16 with BOM, tab-separated
    lines = ["Date\tDescription\tPaid out\tPaid in"]
    lines += [f"{_uk(tx.date)}\t{tx.description}\t{tx.value if tx.out else ''}\t{'' if tx.out else tx.value}"
              for tx in txs]
    return ("\n".join(lines) + "\n").encode("utf-16")


def _xlsx(sheets: dict) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, txs in sheets.items():
        ws = wb.create_sheet(title)
        ws.append(["Date", "Description", "Paid out", "Paid in"])
        for tx in txs:
            ws.append([date.fromisoformat(tx.date), tx.description,
                       float(tx.value) if tx.out else None, None if tx.out else float(tx.value)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Documents ───────────────────────────────────────────────────────────────────

TESCO = """TESCO EXPRESS
123 High Street London
VAT No: GB 220 4302 31
0345 677 9031
03/09/2026 14:22
Meal Deal 3.50
Coffee 2.80
Sandwich 6.20
TOTAL 12.50
CASH 20.00
CHANGE 7.50"""

CAFE = ["THE DAILY GRIND CAFE", "14 Station Road, Leeds", "VAT Reg No 318 4471 92", "12/09/2026 09:41",
        "Flat White        3.40", "Almond Croissant  2.95", "Bottled Water     1.15", "SUBTOTAL          7.50",
        "VAT @ 20%         1.25", "TOTAL             7.50", "CARD              7.50"]

FUEL = ["SHELL WESTWAY", "Pump 4   Unleaded", "45.20 L @ 151.3p/L", "16/09/2026 18:05", "FUEL TOTAL      68.39",
        "VAT 20%         11.40", "TOTAL GBP       68.39", "PAID BY VISA"]

TRAIN = ["TRAINLINE", "E-TICKET RECEIPT", "Order date 17/09/2026", "London Euston to Manchester Piccadilly",
         "Standard Anytime Return x1", "Ticket price 87.50", "Booking fee 0.00", "Total paid £87.50"]

PURCHASE_INVOICE = [
    "Clearway Office Supplies Ltd", "Unit 5, Riverside Park, Bristol BS1 4XE",
    "VAT Registration No: GB 293 7718 05", "", "INVOICE", "Invoice number: CW-20931", "Invoice date: 08/09/2026",
    f"Bill to: {BUSINESS}", "", "A4 copier paper (10 reams)           45.00",
    "Toner cartridge HP 410X             165.00", "Desk organisers x4                   40.00", "",
    "Net amount                          250.00", "VAT at 20%                           50.00",
    "Total due                           300.00",
]

SALES_INVOICE = [
    BUSINESS, "42 Canal Street, Manchester M1 3HU", "VAT Registration No: GB 405 1170 22", "", "INVOICE",
    "Invoice number: NB-0117", "Invoice date: 30/09/2026", "Bill to: Harbour & Lane Architects LLP", "",
    "Strategy consultancy, September 2026    2,000.00", "",
    "Net amount                              2,000.00", "VAT at 20%                                400.00",
    "Total due                               2,400.00",
]

ACCOUNTANT_INVOICE = [
    "Hartley & Co Chartered Accountants", "9 Market Place, York YO1 8SS", "VAT Reg: GB 551 2208 19",
    "INVOICE 2026-311", "Date: 25/09/2026", f"To: {BUSINESS}", "Management accounts   450.00",
    "VAT @ 20%              90.00", "TOTAL                 540.00",
]

STATEMENT = [
    f"{BUSINESS} - Business Current Account", "Sort code 20-45-77   Account 43018822",
    "Statement period 01/09/2026 to 30/09/2026", "",
    "Date        Description                  Money out    Money in     Balance",
    "01/09/2026  Opening balance                                        8,420.15",
    "03/09/2026  BRITISH GAS BUSINESS             86.40                 8,333.75",
    "05/09/2026  LANDMARK PROPERTIES RENT      1,250.00                 7,083.75",
    "09/09/2026  HARBOUR & LANE ARCHITECTS                 3,600.00    10,683.75",
    "20/09/2026  BARCLAYS BANK CHARGES             8.50                10,675.25",
    "28/09/2026  OCTOPUS ENERGY                  142.18                10,533.07",
]

# ── Transactions ────────────────────────────────────────────────────────────────

T_TESCO = (Tx("2026-09-03", "Tesco Express", "-12.50", "7406"),)
T_PROBE_CSV = (Tx("2026-09-01", "BT BUSINESS BROADBAND", "-72.00", "7502"),
               Tx("2026-09-02", "ACME LTD BACS", "3600.00", "4000"),
               Tx("2026-09-04", "SAINSBURYS", "-24.50", "8205"))
T_BARCLAYS = ((Tx("2026-09-02", "VODAFONE LTD", "-45.99", "7502"), "Direct Debit"),
              (Tx("2026-09-10", "STAPLES UK", "-32.40", "7504"), "Card Purchase"),
              (Tx("2026-09-13", "BRIGHTWELL LTD", "950.00", "4000"), "Bank Credit"),
              (Tx("2026-09-18", "ROYAL MAIL", "-15.60", "7501"), "Card Purchase"),
              (Tx("2026-09-22", "PREMIER INN", "-89.00", "7402"), "Card Purchase"),
              (Tx("2026-09-27", "HMRC VAT", "-1210.33", "2202"), "Direct Debit"))
T_HSBC = (Tx("2026-09-03", "BRITISH GAS", "-86.40", "7201"), Tx("2026-09-11", "EE LIMITED", "-28.00", "7502"),
          Tx("2026-09-15", "NORTHWIND TRADING PAYMENT", "1800.00", "4000"),
          Tx("2026-09-24", "HMRC PAYE", "-2140.37", "2210"))
T_LLOYDS = ((Tx("2026-09-04", "SCREWFIX DIRECT", "-54.00", "7800"), "DEB"),
            (Tx("2026-09-08", "QUAYSIDE MEDIA LTD", "2750.00", "4000"), "FPI"),
            (Tx("2026-09-12", "TFL TRAVEL CH", "-12.80", "7400"), "DEB"),
            (Tx("2026-09-19", "XERO UK LTD", "-33.00", "8201"), "DD"))
T_MONZO = ((Tx("2026-09-05", "PRET A MANGER LONDON", "-6.45", "7406"), "09:12:44", "Card payment", "Pret A Manger", "Eating out"),
           (Tx("2026-09-09", "OAKRIDGE DESIGN INV 2026-14", "1450.00", "4000"), "14:03:10", "Faster payment", "Oakridge Design Ltd", "Income"),
           (Tx("2026-09-21", "UBER *TRIP", "-18.20", "7400"), "11:47:02", "Card payment", "Uber", "Transport"),
           (Tx("2026-09-26", "GOOGLE WORKSPACE", "-11.50", "8201"), "08:00:00", "Direct Debit", "Google Workspace", "Bills"))
T_STARLING = ((Tx("2026-09-03", "WeWork Desk rental September", "-295.00", "7100"), "WeWork", "Desk rental September", "DIRECT DEBIT", "BILLS_AND_SERVICES"),
              (Tx("2026-09-07", "Hartley & Co Invoice 2026-288", "-360.00", "7601"), "Hartley & Co", "Invoice 2026-288", "FASTER PAYMENT", "GENERAL"),
              (Tx("2026-09-15", "Brightwell Ltd INV NB-0112", "2100.00", "4000"), "Brightwell Ltd", "INV NB-0112", "FASTER PAYMENT", "INCOME"),
              (Tx("2026-09-23", "Avanti West Coast Rail", "-64.30", "7400"), "Avanti West Coast", "Rail", "CARD", "TRANSPORT"))
T_PREAMBLE = (Tx("2026-09-02", "ADOBE SYSTEMS", "-19.97", "8201"),
              Tx("2026-09-06", "CLIENT PAYMENT - FIELDHOUSE LTD", "1200.00", "4000"),
              Tx("2026-09-14", "COSTA COFFEE", "-8.40", "7406"),
              Tx("2026-09-29", "BARCLAYCARD COMMERCIAL FEES", "-12.00", "7901"))
T_TSV = (Tx("2026-09-03", "VIKING DIRECT STATIONERY", "-23.99", "7504"),
         Tx("2026-09-10", "KINGSTON CATERING LTD", "640.00", "4000"),
         Tx("2026-09-18", "DVLA VEHICLE TAX", "-190.00", "7302"))
T_XLSX = {"Sep 2026": (Tx("2026-09-04", "SAGE SUBSCRIPTION", "-26.40", "8201"),
                       Tx("2026-09-17", "RIVERSIDE DENTAL", "980.00", "4000"),
                       Tx("2026-09-25", "HISCOX INSURANCE", "-41.25", "8204")),
          "Oct 2026": (Tx("2026-10-02", "BT BUSINESS BROADBAND", "-72.00", "7502"),
                       Tx("2026-10-09", "RIVERSIDE DENTAL", "980.00", "4000"),
                       Tx("2026-10-20", "ROYAL MAIL", "-7.85", "7501"))}
T_PASTED_CSV = (Tx("2026-09-08", "MICROSOFT 365 BUSINESS", "-9.60", "8201"),
                Tx("2026-09-11", "ADOBE CREATIVE CLOUD", "-19.97", "8201"),
                Tx("2026-09-12", "CLIENT PAYMENT BRIGHTWELL LTD", "1250.00", "4000"),
                Tx("2026-09-14", "DIRECT LINE BUSINESS INSURANCE", "-38.12", "8204"))
_PAYEES = (("TESCO STORES", "-", "7406"), ("SHELL", "-", "7300"), ("AMAZON MARKETPLACE", "-", "7504"),
           ("CLIENT RECEIPT", "+", "4000"), ("UBER", "-", "7400"), ("VODAFONE", "-", "7502"),
           ("ROYAL MAIL", "-", "7501"), ("PRET A MANGER", "-", "7406"))


def _long_statement() -> tuple[Tx, ...]:
    rng = random.Random(2026)
    txs = []
    for n in range(60):
        name, sign, account = _PAYEES[n % len(_PAYEES)]
        pence = rng.randrange(150, 9000) if sign == "-" else rng.randrange(40000, 250000)
        txs.append(Tx(f"2026-09-{1 + n // 2:02d}", f"{name} {n + 1:03d}",
                      f"{'-' if sign == '-' else ''}{pence // 100}.{pence % 100:02d}", account))
    return tuple(txs)


T_LONG = _long_statement()

CASES: tuple[Case, ...] = (
    # Typed or pasted text (Quick Paste)
    Case("text-tesco-receipt", "text", "input.txt", _text(TESCO), T_TESCO),
    Case("text-hmrc-vat-payment", "text", "input.txt", _text("HMRC VAT settlement payment 1450.00 paid 07/09/2026"),
         (Tx("2026-09-07", "HMRC VAT settlement payment", "-1450.00", "2202"),)),
    Case("text-supplier-refund", "text", "input.txt",
         _text("Refund from Amazon for returned printer 89.99 received 05/09/2026"),
         (Tx("2026-09-05", "Refund from Amazon for returned printer", "89.99", "0030"),)),
    Case("text-paye", "text", "input.txt",
         _text("Paid HMRC PAYE and National Insurance for August: £2,140.37 on 19/09/2026"),
         (Tx("2026-09-19", "HMRC PAYE and National Insurance", "-2140.37", "2210"),)),
    Case("text-savings-transfer", "text", "input.txt",
         _text("Moved £5,000.00 from the current account to the business savings account on 15/09/2026"),
         (Tx("2026-09-15", "Transfer to business savings account", "-5000.00", "1210"),)),
    Case("text-notes", "text", "input.txt", _text(
        "02/09/2026 Consulting services sold to ACME Corp for £2,400.00\n"
        "01/09/2026 BT Business Broadband monthly bill £72.00\n"
        "10/09/2026 Office chair from IKEA £149.99"),
         (Tx("2026-09-02", "Consulting services sold to ACME Corp", "2400.00", "4000"),
          Tx("2026-09-01", "BT Business Broadband monthly bill", "-72.00", "7502"),
          Tx("2026-09-10", "Office chair from IKEA", "-149.99", "0040"))),
    Case("text-pasted-csv", "text", "input.txt", lambda: _statement_csv(T_PASTED_CSV, "5000.00"), T_PASTED_CSV),
    # Photos of receipts
    Case("img-tesco-receipt", "image", "input.png", lambda: _receipt_png(TESCO.splitlines()), T_TESCO),
    Case("img-cafe-receipt-vat", "image", "input.png", lambda: _receipt_png(CAFE),
         (Tx("2026-09-12", "The Daily Grind Cafe", "-7.50", "7406", vat="1.25"),)),
    Case("img-fuel-receipt-rotated", "image", "input.png", lambda: _receipt_png(FUEL, rotate=2.0, noise=True),
         (Tx("2026-09-16", "Shell Westway fuel", "-68.39", "7300", vat="11.40"),)),
    Case("img-train-ticket", "image", "input.png", lambda: _receipt_png(TRAIN),
         (Tx("2026-09-17", "Trainline London Euston to Manchester Piccadilly", "-87.50", "7400"),)),
    # PDFs
    Case("pdf-purchase-invoice", "pdf", "input.pdf", lambda: _text_pdf(PURCHASE_INVOICE),
         (Tx("2026-09-08", "Clearway Office Supplies Ltd", "-300.00", "7504", vat="50.00"),)),
    Case("pdf-sales-invoice", "pdf", "input.pdf", lambda: _text_pdf(SALES_INVOICE),
         (Tx("2026-09-30", "Harbour & Lane Architects LLP", "2400.00", "4000", vat="400.00"),)),
    Case("pdf-scanned-invoice", "pdf", "input.pdf", lambda: _scanned_pdf(ACCOUNTANT_INVOICE),
         (Tx("2026-09-25", "Hartley & Co Chartered Accountants", "-540.00", "7601", vat="90.00"),)),
    Case("pdf-bank-statement", "pdf", "input.pdf", lambda: _text_pdf(STATEMENT),
         (Tx("2026-09-03", "BRITISH GAS BUSINESS", "-86.40", "7201"),
          Tx("2026-09-05", "LANDMARK PROPERTIES RENT", "-1250.00", "7100"),
          Tx("2026-09-09", "HARBOUR & LANE ARCHITECTS", "3600.00", "4000"),
          Tx("2026-09-20", "BARCLAYS BANK CHARGES", "-8.50", "7901"),
          Tx("2026-09-28", "OCTOPUS ENERGY", "-142.18", "7200"))),
    # Bank exports
    Case("csv-barclays-probe", "table", "input.csv", lambda: _statement_csv(T_PROBE_CSV, "1306.56"), T_PROBE_CSV),
    Case("csv-barclays-export", "table", "input.csv", lambda: _barclays_export(T_BARCLAYS),
         tuple(tx for tx, _ in T_BARCLAYS)),
    Case("csv-hsbc-no-header", "table", "input.csv", lambda: _hsbc(T_HSBC), T_HSBC),
    Case("csv-lloyds-debit-credit", "table", "input.csv", lambda: _lloyds(T_LLOYDS, "6174.33"),
         tuple(tx for tx, _ in T_LLOYDS)),
    Case("csv-monzo", "table", "input.csv", lambda: _monzo(T_MONZO), tuple(entry[0] for entry in T_MONZO)),
    Case("csv-starling", "table", "input.csv", lambda: _starling(T_STARLING, "3200.00"),
         tuple(entry[0] for entry in T_STARLING)),
    Case("csv-preamble-cp1252-brackets", "table", "input.csv", lambda: _preamble_cp1252(T_PREAMBLE, "5000.00"),
         T_PREAMBLE),
    Case("tsv-utf16-unicode-text", "table", "input.tsv", lambda: _utf16_tsv(T_TSV), T_TSV),
    Case("xlsx-two-months", "table", "input.xlsx", lambda: _xlsx(T_XLSX), T_XLSX["Sep 2026"] + T_XLSX["Oct 2026"]),
    Case("csv-long-statement-60-rows", "table", "input.csv", lambda: _statement_csv(T_LONG, "20000.00"), T_LONG),
)
```

`eval/make_fixtures.py`:

```python
"""Writes the synthetic accuracy fixtures in eval/fixtures/ from eval_cases.CASES.

Run after editing eval_cases.py:  .venv/bin/python eval/make_fixtures.py"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from eval_cases import CASES

FIXTURES = Path(__file__).parent / "fixtures"


def write_all(target: Path = FIXTURES) -> list[Path]:
    if target.exists():
        shutil.rmtree(target)
    written = []
    for case in CASES:
        folder = target / case.id
        folder.mkdir(parents=True)
        (folder / case.filename).write_bytes(case.render())
        expected = {"case": case.id, "kind": case.kind, "input": case.filename,
                    "rows": [tx.expected for tx in case.transactions]}
        (folder / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
        written.append(folder)
    return written


if __name__ == "__main__":
    print(f"Wrote {len(write_all())} fixtures to {FIXTURES}")
```

- [ ] **Step 4: Generate the fixtures.** Run `.venv/bin/python eval/make_fixtures.py`. Expected: `Wrote 25 fixtures to …/eval/fixtures`.

- [ ] **Step 5: Run and confirm pass.** Run `.venv/bin/python -m pytest tests/test_eval_fixtures.py -q`. Expected: all pass. Image cases and the scanned PDF are skipped only in the "amount is printed" test.

- [ ] **Step 6: Commit**

```bash
git add eval/eval_cases.py eval/make_fixtures.py eval/fixtures tests/test_eval_fixtures.py
git commit -m "Add 25 synthetic UK fixtures with expected transactions"
```

---

### Task 3: Black-box runner

**Files:**
- Create: `eval/run_eval.py`
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes:
  - Task 1: `adapt_rows`, `expected_rows`, `score_case`, `summarise`.
  - Task 2: the `expected.json` layout.
  - Phase 1 HTTP API: `POST /api/analyze` → 202 `{job_id}`, `GET`/`DELETE /api/jobs/{id}`, `POST /api/trial-balance`, `GET /api/health`.
- Produces:
  - `load_cases(root, pattern="") -> list[dict]`
  - `run_document(client, path, timeout) -> (result | None, error_code | None)`
  - `run_case(client, case, timeout) -> dict`
  - `results_dir(private: bool) -> Path`
  - `main(argv=None) -> int`

- [ ] **Step 1: Write the failing tests** — `tests/test_eval_runner.py`:

```python
import subprocess
from pathlib import Path

import pytest
from fakes import FakeOcr, FakeOllama, transactions_json
from fastapi.testclient import TestClient

from ledgersync.config import Settings
from run_eval import load_cases, main, results_dir, run_case
from server import create_app

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"
HMRC_ROW = {"description": "HMRC VAT settlement payment", "date": "2026-09-07", "amount": 1450.0,
            "currency": "GBP", "type": "expense", "account": "VAT"}


@pytest.fixture
def api():
    clients = []

    def _make(ollama):
        client = TestClient(create_app(Settings(warmup=False), ollama=ollama, ocr=FakeOcr()))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def test_load_cases_filters_by_id():
    cases = load_cases(FIXTURES, "csv-")
    assert cases and all(c["case"].startswith("csv-") and c["path"].exists() for c in cases)


def test_case_is_run_through_the_api_and_scored(api):
    case = load_cases(FIXTURES, "text-hmrc-vat-payment")[0]
    outcome = run_case(api(FakeOllama([transactions_json(HMRC_ROW)])), case, timeout=10)
    assert (outcome["matched"], outcome["expected"], outcome["error"]) == (1, 1, None)
    assert outcome["checks"]["direction"] == [1, 1] and outcome["exact"] is True
    assert outcome["model"] == "fake-model" and outcome["latency_s"] >= 0
    assert outcome["tb_balanced"] in (True, False)


def test_failed_case_is_recorded_not_fatal(api):
    case = load_cases(FIXTURES, "text-hmrc-vat-payment")[0]
    outcome = run_case(api(FakeOllama(healthy=False)), case, timeout=10)
    assert outcome["error"] == "ai_offline"
    assert (outcome["matched"], outcome["predicted"], outcome["tb_balanced"]) == (0, 0, None)


def test_unreachable_api_exits_with_advice(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("run_eval.EVAL_DIR", tmp_path)
    (tmp_path / "fixtures").symlink_to(FIXTURES)
    assert main(["--api", "http://127.0.0.1:9", "--label", "t"]) == 2
    assert "server.py" in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_private_results_are_git_ignored():
    target = results_dir(private=True) / "2026-01-01-check.json"
    assert subprocess.run(["git", "check-ignore", "-q", str(target)]).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", str(results_dir(private=False) / "x.json")]).returncode == 1
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_eval_runner.py -q`. Expected: `ModuleNotFoundError: No module named 'run_eval'`.

- [ ] **Step 3: Implement** — `eval/run_eval.py`:

```python
"""Measures extraction accuracy against a running LedgerSync API, treated as a black box.

  .venv/bin/python server.py                                  # in one terminal
  .venv/bin/python eval/run_eval.py --label my-change         # in another
  .venv/bin/python eval/run_eval.py --label real --private    # anonymised docs in eval/private/

Writes eval/results/<date>-<label>.json (private runs: eval/private/results/, never committed)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

from eval_scoring import adapt_rows, expected_rows, score_case, summarise

EVAL_DIR = Path(__file__).parent
POLL_SECONDS = 0.5


def results_dir(private: bool) -> Path:
    return EVAL_DIR / "private" / "results" if private else EVAL_DIR / "results"


def load_cases(root: Path, pattern: str = "") -> list[dict]:
    cases = []
    for expected_file in sorted(root.glob("*/expected.json")):
        spec = json.loads(expected_file.read_text())
        if pattern in spec["case"]:
            cases.append({**spec, "path": expected_file.parent / spec["input"]})
    return cases


def _error_code(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    return detail["code"] if isinstance(detail, dict) and "code" in detail else f"http_{resp.status_code}"


def run_document(client, path: Path, timeout: float) -> tuple[Optional[dict], Optional[str]]:
    """Sends one document through POST /api/analyze and the job API. .txt goes in as pasted text."""
    if path.suffix == ".txt":
        resp = client.post("/api/analyze", data={"text": path.read_text(encoding="utf-8")})
    else:
        resp = client.post("/api/analyze", files={"file": (path.name, path.read_bytes())})
    if resp.status_code != 202:
        return None, _error_code(resp)
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "succeeded":
            return job["result"], None
        if job["status"] in ("failed", "cancelled"):
            return None, (job.get("error") or {}).get("code", job["status"])
        time.sleep(POLL_SECONDS)
    client.delete(f"/api/jobs/{job_id}")
    return None, "eval_timeout"


def _trial_balance_ok(client, result: dict) -> Optional[bool]:
    rows = result.get("transactions") if result.get("transactions") is not None else result.get("data")
    if not rows:
        return None
    resp = client.post("/api/trial-balance", json=rows)
    return bool(resp.json().get("is_balanced")) if resp.status_code == 200 else None


def run_case(client, case: dict, timeout: float) -> dict:
    started = time.monotonic()
    result, error = run_document(client, case["path"], timeout)
    latency = round(time.monotonic() - started, 1)
    predicted = adapt_rows(result) if result else []
    return {
        "case": case["case"], "kind": case["kind"], "error": error, "latency_s": latency,
        "model": (result or {}).get("model"),
        "tb_balanced": _trial_balance_ok(client, result) if result else None,
        **score_case(expected_rows(case["rows"]), predicted),
        "predicted": predicted,
    }


def _print_summary(report: dict) -> None:
    head = ("cases", "prec", "recall", "amount", "date", "dir", "acct", "vat", "exact", "TB ok", "p50 s", "p95 s")
    print("\n" + " " * 8 + " ".join(f"{h:>6}" for h in head))
    for name, s in [("all", report["summary"]), *report["by_kind"].items()]:
        f = s["field_accuracy"]
        cells = (s["cases"], s["row_precision"], s["row_recall"], f["amount"], f["date"], f["direction"],
                 f["account"], f["vat"], s["exact_cases"], s["tb_balanced"], s["latency_p50_s"], s["latency_p95_s"])
        print(f"{name:8}" + " ".join(f"{'n/a' if c is None else c:>6}" for c in cells))
    if report["summary"]["errors"]:
        print("errors:", report["summary"]["errors"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8085")
    parser.add_argument("--label", required=True, help="name for this run, e.g. baseline")
    parser.add_argument("--cases", default="", help="only cases whose id contains this text")
    parser.add_argument("--private", action="store_true", help="run eval/private/ instead of the fixtures")
    parser.add_argument("--timeout", type=float, default=600, help="seconds allowed per document")
    args = parser.parse_args(argv)

    import httpx2

    cases = load_cases(EVAL_DIR / ("private" if args.private else "fixtures"), args.cases)
    if not cases:
        print("No cases found.", file=sys.stderr)
        return 1
    with httpx2.Client(base_url=args.api, timeout=180) as client:
        try:
            health = client.get("/api/health").json()
        except Exception:
            print(f"Cannot reach the LedgerSync API at {args.api}. Start it with: .venv/bin/python server.py",
                  file=sys.stderr)
            return 2
        results = []
        for n, case in enumerate(cases, start=1):
            print(f"[{n}/{len(cases)}] {case['case']} ...", end=" ", flush=True)
            outcome = run_case(client, case, args.timeout)
            results.append(outcome)
            print(outcome["error"] or f"{outcome['matched']}/{outcome['expected']} rows matched,"
                  f" {outcome['predicted']} predicted", f"({outcome['latency_s']} s)", flush=True)
    report = {
        "label": args.label, "date": date.today().isoformat(), "api": args.api, "model": health.get("model"),
        "summary": summarise(results),
        "by_kind": {kind: summarise([r for r in results if r["kind"] == kind])
                    for kind in sorted({r["kind"] for r in results})},
        "cases": results,
    }
    out_dir = results_dir(args.private)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{report['date']}-{args.label}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n")
    _print_summary(report)
    print(f"\nWrote {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run and confirm pass.** Run `.venv/bin/python -m pytest tests/test_eval_runner.py -q`. Expected: 5 passed.

- [ ] **Step 5: Run the whole suite.** Run `.venv/bin/python -m pytest -q`. Expected: all pass (the `llm` tests are deselected).

- [ ] **Step 6: Commit**

```bash
git add eval/run_eval.py tests/test_eval_runner.py
git commit -m "Add black-box accuracy runner with per-kind report"
```

---

### Task 4: Baseline run and docs

**Files:**
- Create: `eval/results/2026-09-28-baseline.json` (generated)
- Modify: `README.md`

- [ ] **Step 1: Run the baseline.**
  - Stop the Next dev server; memory is tight on 8 GB.
  - Start only the API (`api` launch config).
  - Run `.venv/bin/python eval/run_eval.py --label baseline` in the background and wait for it to finish.
  - Expected: 25 progress lines, then the summary table, then `Wrote …/eval/results/2026-09-28-baseline.json`.
  - Cases may fail with error codes (for example `ai_output_truncated` on the 60-row statement). That is baseline data, not a harness failure.

- [ ] **Step 2: Unload the model.** Stop the API and run `ollama stop qwen2.5vl:3b` to give the memory back.

- [ ] **Step 3: README section.** Add after "## Tests":

````markdown
## Measuring accuracy

`eval/` holds 25 synthetic UK documents (receipts, invoices, bank statements in several bank
formats, spreadsheets and pasted text), each with the transactions a bookkeeper would record
(`eval/fixtures/*/expected.json`), and a harness that runs them through a running API:

```bash
.venv/bin/python server.py                               # in one terminal
.venv/bin/python eval/run_eval.py --label my-change      # in another
```

It prints row precision and recall, per-field accuracy (amount, date and direction; account
and VAT once the API returns them), how often the trial balance balances, and latency, and
writes `eval/results/<date>-<label>.json`. Compare it with `eval/results/2026-09-28-baseline.json`.

To measure real documents, put anonymised copies in `eval/private/<case>/` (the input file plus
an `expected.json` in the same format) and add `--private`; that folder is never committed.
After editing `eval/eval_cases.py`, regenerate the fixtures with `.venv/bin/python eval/make_fixtures.py`.
````

- [ ] **Step 4: Commit**

```bash
git add eval/results/2026-09-28-baseline.json README.md
git commit -m "Record Phase 2 accuracy baseline"
```
