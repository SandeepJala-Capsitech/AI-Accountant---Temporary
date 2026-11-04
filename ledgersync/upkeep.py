"""Keeping a client's books up to date after a change: the rows matching dated from the bank are recorded, the rule
questions are worked out again and the year is posted. A person's change is recorded first and never fails because the
upkeep after it did."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Sequence

from . import books, ledger_book, questions
from .agent.locks import ClientLocks
from .models import ChangeDraft, PostResult
from .records import AgentRecords
from .resolutions import record_automatic
from .store_mongo import MongoStore

logger = logging.getLogger(__name__)


def settle(store: MongoStore, records: AgentRecords, client_id: int, year_start: dt.date, *,
           thread_id: Optional[str] = None, decision_ids: Sequence[str] = ()) -> PostResult:
    """With the client's lock held by the caller: records the rows matching dated from the bank, works the year's
    questions out and posts the year, with the decisions behind the posting."""
    record_automatic(store, records, client_id=client_id, year_start=year_start, thread_id=thread_id)
    questions.refresh(store, records, client_id, year_start)
    return ledger_book.post(store, records, client_id, year_start, decision_ids=decision_ids)


def after_change(store: MongoStore, records: AgentRecords, locks: ClientLocks, change: ChangeDraft, *,
                 today: Optional[dt.date] = None) -> dict[dt.date, PostResult]:
    """Records the change, then settles every year of the client that isn't closed, each under the client's lock.
    A year whose settling fails is logged and left out of the result; nothing is raised once the change is recorded."""
    records.add_change(change)
    results: dict[dt.date, PostResult] = {}
    try:
        years = [y.start for y in books.years_of(store, change.client_id, today or dt.date.today()) if not y.closed]
    except Exception:
        logger.exception("Couldn't list the years of client %s to settle", change.client_id)
        return results
    for start in years:
        try:
            with locks.hold(change.client_id):
                results[start] = settle(store, records, change.client_id, start)
        except Exception:
            logger.exception("Couldn't settle client %s, year %s", change.client_id, start)
    return results
