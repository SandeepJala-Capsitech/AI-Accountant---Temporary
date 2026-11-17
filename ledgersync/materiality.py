"""How much is too little to ask about, for one client and year: its materiality, worked out from its own figures as
an auditor's method does it (ISA (UK) 320 and 450), as the app's policy, or set by the firm.

Overall materiality is 5% of profit before tax when there is a profit, else 1% of total costs (ISA 320 A8). The
working level (performance materiality) is half of it, the prudent end of the 50-75% firms use; the trivial level is
3% of it, inside the 0-5% every firm the FRC reviewed uses. Below the trivial level a presentation choice is left as
coded and said so; facts that change the tax are asked whatever the amount (checks)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from pydantic import BaseModel

from .accounts import BY_CODE, AccountType
from .checks import booked
from .models import Transaction, Year

PROFIT_SHARE, COSTS_SHARE = Decimal("0.05"), Decimal("0.01")
WORKING_SHARE, TRIVIAL_SHARE = Decimal("0.50"), Decimal("0.03")
PENNY = Decimal("0.01")


class Levels(BaseModel):
    """A client's levels for a year, and what they were worked out from."""
    overall: Decimal
    working: Decimal                        # performance materiality: small items left add up to this, they are asked
    trivial: Decimal                        # below this a presentation choice is left as coded
    benchmark: Literal["profit", "costs", "firm"]
    figure: Optional[Decimal] = None        # the profit or costs it is a share of (a year's worth)
    basis: str                              # in plain words, for the page and the file


def _pounds(amount: Decimal) -> Decimal:
    return amount.quantize(PENNY, rounding=ROUND_HALF_UP)


def levels(income: Decimal, costs: Decimal, *, months: int = 12, which: str,
           firm: Optional[Decimal] = None) -> Optional[Levels]:
    """The levels from a year's booked income and costs (net of VAT) over `months` months; `which` says which year
    ("last year", "this year so far"). None when there are no figures: then nothing is trivial."""
    if firm is not None and firm > 0:
        overall = _pounds(firm)
        return Levels(overall=overall, working=_pounds(overall * WORKING_SHARE),
                      trivial=_pounds(overall * TRIVIAL_SHARE), benchmark="firm", basis="set by the firm")
    if income <= 0 and costs <= 0:
        return None
    scale = Decimal(12) / Decimal(max(months, 1))
    income, costs = income * scale, costs * scale
    when = which if months >= 12 else f"{which}, scaled to 12 months"
    profit = income - costs
    if profit > 0:
        overall, benchmark, figure = profit * PROFIT_SHARE, "profit", _pounds(profit)
        basis = f"5% of profit before tax, £{figure:,.2f}, {when}"
    else:
        overall, benchmark, figure = costs * COSTS_SHARE, "costs", _pounds(costs)
        basis = f"1% of total costs, £{figure:,.2f}, {when}"
    overall = _pounds(overall)
    if overall <= 0:
        return None
    return Levels(overall=overall, working=_pounds(overall * WORKING_SHARE), trivial=_pounds(overall * TRIVIAL_SHARE),
                  benchmark=benchmark, figure=figure, basis=basis)


def _figures(rows: list[Transaction], placed: list[int], n: int) -> tuple[Decimal, Decimal, Optional[int]]:
    """Year n's booked income and costs, net of VAT, and the month of the year its latest dated row is in (1-12)."""
    income = costs = Decimal(0)
    latest = None
    for tx, p in zip(rows, placed):
        account = BY_CODE.get(tx.account_code)
        if p != n or account is None or not booked(tx):
            continue
        net = tx.gross - (tx.vat_posted or 0)
        if account.type == AccountType.INCOME:
            income += net if tx.direction.value == "in" else -net
        elif account.type == AccountType.EXPENSE:
            costs += net if tx.direction.value == "out" else -net
        else:
            continue
        if tx.date and (latest is None or tx.date > latest):
            latest = tx.date
    return income, costs, latest


def year_levels(rows: list[Transaction], placed: list[int], years: list[Year], n: int,
                firm: Optional[Decimal]) -> Optional[Levels]:
    """Year n's levels: from the year before when it has figures, else from year n so far, scaled to 12 months;
    the firm's figure, when it set one, instead."""
    if firm is not None and firm > 0:
        return levels(Decimal(0), Decimal(0), which="", firm=firm)
    if n > 0:
        income, costs, latest = _figures(rows, placed, n - 1)
        if latest is not None:
            return levels(income, costs, which="last year")
    income, costs, latest = _figures(rows, placed, n)
    if latest is None:
        return None
    start = years[n].start
    months = (latest.year - start.year) * 12 + latest.month - start.month + 1
    return levels(income, costs, months=min(max(months, 1), 12), which="this year so far")


def decide(rows: list[Transaction], placed: list[int],
           levels_by_year: dict[int, Optional[Levels]]) -> list[Transaction]:
    """Each presentation question checks.normalise found (checks.PRESENTATION), asked or left by the levels of the
    row's year: asked from the trivial level (equipment kept as a cost from the working level, a small asset under
    the trivial level), else left as coded with a note saying so. With no levels, nothing is trivial: all asked.
    The items left on an account in a year are added up, and asked about together when they reach the working
    level."""
    from .checks import PRESENTATION, issue   # checks imports nothing from here; kept local for clarity
    out = list(rows)
    left: dict[tuple[int, str], list[tuple[int, Decimal]]] = {}
    for k, tx in enumerate(rows):
        lv = levels_by_year.get(placed[k])
        kept = [i for i in tx.issues if i.code not in PRESENTATION]
        for found in tx.presentation if booked(tx) else []:   # a row not booked (a claim line its receipt books) asks nothing
            stake = found.difference if found.difference is not None else tx.gross
            if found.code == "small_asset":   # an asset small for this client: asked; otherwise fine as it is
                if lv is None or stake < lv.trivial:
                    kept.append(found)
                continue
            bar, name = ((lv.working, "working level") if found.code == "asset_check" else (lv.trivial, "trivial level")) \
                if lv is not None else (None, "")
            if bar is None or stake >= bar:
                kept.append(found.model_copy(update={"severity": "warning"}))
                continue
            account = BY_CODE.get(tx.account_code)
            kept.append(issue("left_small", f"£{stake:,.2f} is under this client's {name} of £{bar:,.2f}: left on "
                                            f"{account.name if account else tx.account_code} as coded.", "info"))
            left.setdefault((placed[k], tx.account_code), []).append((k, stake))
        out[k] = tx.model_copy(update={"issues": kept, "presentation": []})
    for (year, code), items in left.items():
        lv = levels_by_year.get(year)
        total = sum((stake for _, stake in items), Decimal(0))
        if (lv is None or len(items) < 2 or total < lv.working
                or any("small_items_add_up" in rows[k].accepted for k, _ in items)):   # a person said they're fine
            continue
        biggest = max(items, key=lambda item: item[1])[0]
        account = BY_CODE.get(code)
        said = issue("small_items_add_up", f"{len(items)} small items left on {account.name if account else code} add "
                                           f"up to £{total:,.2f}, over this client's working level of "
                                           f"£{lv.working:,.2f}: check them together.",
                     related=[k for k, _ in items if k != biggest])
        out[biggest] = out[biggest].model_copy(update={"issues": [*out[biggest].issues, said]})
    return out
