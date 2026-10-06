"""Scoring for the accuracy harness: turns the API's results into comparable rows, pairs them with
the expected rows and computes metrics. Pure functions, no I/O."""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

FIELDS = ("amount", "date", "direction", "account", "vat", "document_type")
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
    """The API's {"transactions": [...]} as comparable rows. A transaction's "vat" is only what the
    document showed, so the booked "vat_posted" is scored."""
    return [{
        "date": normalise_date(row.get("date")),
        "direction": row.get("direction"),
        "gross": to_money(row.get("gross")),
        "vat": to_money(row["vat_posted"] if "vat_posted" in row else row.get("vat")),
        "account_code": row.get("account_code"),
        "description": str(row.get("description") or ""),
        "has_account": "account_code" in row,
        "has_vat": "vat_posted" in row or "vat" in row,
        "document_type": row.get("document_type"),
        "has_type": "document_type" in row,
    } for row in result.get("transactions") or []]


def _overlap(a: dict, b: dict) -> float:
    ta, tb = (set(re.findall(r"[a-z0-9]{3,}", r.get("description", "").lower())) for r in (a, b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _same_amount(e: dict, p: dict) -> bool:
    return p["gross"] is not None and p["gross"] == e["gross"]


def _same_date(e: dict, p: dict) -> bool:
    return p["date"] is not None and p["date"] == e["date"]


def match_rows(expected: list[dict], predicted: list[dict]) -> list[tuple[int, int]]:
    """Greedy one-to-one pairing, best pairs first. A pair must share the amount, or the date
    plus the direction or a similar description. Pairing a same-date row with the wrong amount
    matters: otherwise a misread amount (e.g. net instead of gross) never counts as an amount
    error and only lowers recall."""
    candidates = []
    for i, e in enumerate(expected):
        for j, p in enumerate(predicted):
            if _same_amount(e, p) or (_same_date(e, p) and (p["direction"] == e["direction"]
                                                            or _overlap(e, p) >= 0.3)):
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
        if e.get("document_type") and p.get("has_type"):
            check("document_type", p["document_type"] == e["document_type"])
    exact = (len(pairs) == len(expected) == len(predicted)
             and all(correct == scored for correct, scored in checks.values()))
    # Rows a bookkeeper could post as-is: right amount, date and direction.
    correct = sum(1 for i, j in pairs if _same_amount(expected[i], predicted[j])
                  and predicted[j]["date"] == expected[i]["date"]
                  and predicted[j]["direction"] == expected[i]["direction"])
    return {"expected": len(expected), "predicted": len(predicted), "matched": len(pairs),
            "correct": correct, "checks": checks, "exact": exact}


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
        # End to end: share of all expected transactions extracted fully right. Field accuracy
        # below only covers rows that were found at all.
        "correct_rows": _ratio(sum(c["correct"] for c in cases), totals["expected"]),
        "field_accuracy": {f: _ratio(sum(c["checks"][f][0] for c in cases), sum(c["checks"][f][1] for c in cases))
                           for f in FIELDS},
        "exact_cases": _ratio(sum(1 for c in cases if c["exact"]), len(cases)),
        "tb_balanced": _ratio(sum(tb), len(tb)),
        "latency_p50_s": _percentile(latencies, 50),
        "latency_p95_s": _percentile(latencies, 95),
        "errors": dict(Counter(c["error"] for c in cases if c.get("error"))),
    }
