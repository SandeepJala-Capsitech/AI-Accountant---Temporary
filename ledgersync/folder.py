"""A client's folder on the computer the site runs on: which of its files are new since the last import. A file is
new by its contents, not its name, so a renamed or copied file is not read again."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .intake import EXTENSIONS


@dataclass
class FolderScan:
    new: list[tuple[Path, bytes, str]] = field(default_factory=list)   # path, bytes and sha256, one per contents
    skipped: list[str] = field(default_factory=list)                   # names already known, or repeating a new file
    unreadable: list[str] = field(default_factory=list)                # names of a type that can't be read, or that
                                                                       # the computer wouldn't open


def new_files(root: Path, known: set[str]) -> FolderScan:
    """Walks root, hidden files and folders left out, in sorted path order. known: the sha256 of the files already
    imported. Names are paths from root."""
    scan, seen = FolderScan(), set(known)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts) or not path.is_file():
            continue
        name = relative.as_posix()
        if path.suffix.lower() not in EXTENSIONS:
            scan.unreadable.append(name)
            continue
        try:
            data = path.read_bytes()
        except OSError:
            scan.unreadable.append(name)
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            scan.skipped.append(name)
        else:
            seen.add(digest)
            scan.new.append((path, data, digest))
    return scan


# Words a file name uses for each kind of account, and words too common in account names to tell two apart.
_KIND_WORDS = {"current": {"current", "business", "chequing", "checking"},
               "savings": {"savings", "saver", "deposit", "reserve"},
               "credit_card": {"card", "credit", "amex", "visa", "mastercard", "barclaycard"},
               "petty_cash": {"petty", "cash"}}
_COMMON = {"account", "bank", "the", "ltd", "limited", "business"} | {w for words in _KIND_WORDS.values() for w in words}


def account_for(name: str, accounts: Sequence, default: Optional[str]) -> Optional[str]:
    """The client's account or card a file's name points at ("amex-card-jan.csv" at "Amex card"), by the words of
    each account's name and of its kind found in the file's name; default when none stands out. accounts: the
    client's, each with a code, a name and a kind."""
    words = set(re.findall(r"[a-z]+", name.lower()))
    scores = {}
    for account in accounts:
        own = {w for w in re.findall(r"[a-z]+", account.name.lower()) if len(w) >= 3 and w not in _COMMON}
        named = len(own & words)
        scores[account.code] = named * 2 + bool(_KIND_WORDS.get(account.kind, set()) & words)
    best = max(scores.values(), default=0)
    leaders = [code for code, score in scores.items() if score == best]
    return leaders[0] if best > 0 and len(leaders) == 1 else default
