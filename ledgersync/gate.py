"""The quality gate: six checks a client's ledger and trial balance must pass before the job's trial balance goes
for review. They are plain code, so the same rows pass or fail the same way every time. The fix agent works on
the checks that fail; whatever still fails after its last round goes to the reviewer as an exception.

The checks, always in this order:

- trial_balance_builds: the trial balance can be built (no booked row has an error).
- balanced: its debits equal its credits.
- statements_reconcile: every bank statement's rows reach the balances it prints.
- no_errors: no row has an error.
- no_suspense: no booked row posts to 9998 Suspense.
- dates_in_period: every booked row is dated within the period.

A check that fails says what is wrong in a sentence a person reads (`detail`), with the rows it is about."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal, Union, get_args

from pydantic import BaseModel, Field

from . import ledger as books
from .accounts import BY_CODE, SUSPENSE
from .checks import booked
from .errors import InvalidTransactions
from .models import Ledger, SavedRow, TrialBalance
from .notify import long_date
from .store import Store

CheckCode = Literal["trial_balance_builds", "balanced", "statements_reconcile", "no_errors", "no_suspense",
                    "dates_in_period"]
CHECK_CODES: tuple[str, ...] = get_args(CheckCode)
# The issues check_statement puts on the bank lines where a statement stops adding up.
GAP_ISSUES = frozenset({"statement_gap", "statement_total"})


class GateCheck(BaseModel):
    """One check: whether it passed, what is wrong when it didn't (empty when it passed), and the rows it is about."""

    code: CheckCode
    passed: bool
    detail: str
    row_ids: list[int] = Field(default_factory=list)


class GateResult(BaseModel):
    """The gate's six checks, in CHECK_CODES order; it passes only when every check does."""

    passed: bool
    checks: list[GateCheck]


def gbp(amount: Decimal) -> str:
    """Money as the gate writes it: £6.15, £1,200.00, -£6.15."""
    return f"£{amount:,.2f}" if amount >= 0 else f"-£{-amount:,.2f}"


def balance_for(store: Store, client_id: int) -> Union[TrialBalance, list[str]]:
    """The client's trial balance, or the problems that stop it being built."""
    try:
        return books.trial_balance(store, client_id)
    except InvalidTransactions as exc:
        return exc.problems


def gate_for(store: Store, client_id: int, period_start: dt.date, period_end: dt.date) -> GateResult:
    """The gate run on the client's rows as they are now."""
    return run_quality_gate(books.build(store, client_id), balance_for(store, client_id), period_start, period_end)


def gap_rows(ledger: Ledger, upload_id: int) -> list[SavedRow]:
    """The bank lines of one upload where its statement stops adding up, in row ID order."""
    return sorted((r for r in ledger.transactions
                   if r.upload_id == upload_id and any(i.code in GAP_ISSUES for i in r.issues)), key=lambda r: r.id)


def _check(code: str, failed: bool, detail: str, rows=()) -> GateCheck:
    if not failed:
        return GateCheck(code=code, passed=True, detail="")
    return GateCheck(code=code, passed=False, detail=detail, row_ids=sorted(r.id for r in rows))


def _plural(n: int, one: str, many: str) -> str:
    """`one` (which says "1") when n is 1, otherwise n and `many`."""
    return one if n == 1 else f"{n} {many}"


def _in_suspense(row: SavedRow) -> bool:
    """Whether a row posts to Suspense: on its own side (the account it is booked against, which for a bank line
    that pays a document is the account holding what was owed) or on its other side."""
    return SUSPENSE in (row.paid_against or row.account_code, row.contra_account_code)


def run_quality_gate(ledger: Ledger, balance: Union[TrialBalance, list[str]], period_start: dt.date,
                     period_end: dt.date) -> GateResult:
    """The six checks on a client's ledger and its trial balance, or the problems that stopped the trial balance
    being built (a list of them, from InvalidTransactions). no_suspense and dates_in_period look only at the rows
    that are booked: a quote nobody ticked to include can stay in Suspense, or out of the period."""
    rows = ledger.transactions
    on_books = [r for r in rows if booked(r)]
    built = isinstance(balance, TrialBalance)

    if built:
        unequal = balance.total_debits != balance.total_credits
        balanced = _check("balanced", unequal,
                          f"Debits {gbp(balance.total_debits)} and credits {gbp(balance.total_credits)} differ by "
                          f"{gbp(abs(balance.total_debits - balance.total_credits))}.")
    else:
        balanced = _check("balanced", True,
                          "The trial balance can't be built, so its debits and credits can't be compared.")

    gaps = [u for u in ledger.uploads if u.statement is not None and u.statement.status == "gap"]
    gap_text = " ".join(f"{u.name} doesn't add up by {gbp(u.statement.difference)}."
                        if u.statement.difference is not None else f"{u.name} doesn't add up." for u in gaps)

    with_error = [r for r in rows if any(i.severity == "error" for i in r.issues)]
    in_suspense = [r for r in on_books if _in_suspense(r)]
    suspense = f"{SUSPENSE} {BY_CODE[SUSPENSE].name}"
    outside = [r for r in on_books if r.date is None or not period_start <= r.date <= period_end]
    period = f"{long_date(period_start)} to {long_date(period_end)}"
    n_outside = len(outside)

    checks = [
        _check("trial_balance_builds", not built,
               "" if built else "; ".join(balance) or "The trial balance can't be built."),
        balanced,
        _check("statements_reconcile", bool(gaps), gap_text, [r for u in gaps for r in gap_rows(ledger, u.id)]),
        _check("no_errors", bool(with_error),
               f"{_plural(len(with_error), '1 row has', 'rows have')} an error.", with_error),
        _check("no_suspense", bool(in_suspense),
               f"{_plural(len(in_suspense), '1 booked row is', 'booked rows are')} in {suspense}.", in_suspense),
        _check("dates_in_period", bool(outside),
               f"{_plural(n_outside, '1 booked row is', 'booked rows are')} dated outside {period}, or "
               f"{'has' if n_outside == 1 else 'have'} no date.", outside),
    ]
    return GateResult(passed=all(c.passed for c in checks), checks=checks)
