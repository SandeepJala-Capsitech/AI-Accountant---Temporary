"""Matches bank lines to the open invoices and claims they pay (design of 2026-10-06). Pure functions:
the same rows give the same matches, whatever order they arrived in."""
from __future__ import annotations

import re
from typing import Optional

# Words that say nothing about who a business is: legal forms, bank-statement noise and generic trade words.
_IGNORED = {"ltd", "limited", "plc", "llp", "co", "the", "and",
            "bank", "payment", "payments", "fin", "card", "dd", "so", "bacs", "fps", "ref",
            "services", "solutions", "group", "uk", "online", "international", "holdings", "company", "trading"}


def _words(name: Optional[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _IGNORED]


def _abbreviates(short: str, long: str) -> bool:
    """'mgmt' abbreviates 'management': three letters or more, the same first letter, the rest in order."""
    if not 3 <= len(short) < len(long) or short[0] != long[0]:
        return False
    letters = iter(long)
    return all(ch in letters for ch in short)


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two names share a word of four or more letters, or a word of one abbreviates a word of
    the other, after legal forms, bank words, generic trade words and numbers are set aside."""
    wa, wb = _words(a), _words(b)
    return (any(w in wb for w in wa if len(w) >= 4)
            or any(_abbreviates(x, y) or _abbreviates(y, x) for x in wa for y in wb))
