"""A client's folder on the computer the site runs on: which of its files are new since the last import. A file is
new by its contents, not its name, so a renamed or copied file is not read again."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

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
