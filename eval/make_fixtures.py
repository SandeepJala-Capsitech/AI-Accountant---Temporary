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
                    "rows": [{**tx.expected, "document_type": case.document_type} for tx in case.transactions]}
        (folder / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
        written.append(folder)
    return written


if __name__ == "__main__":
    print(f"Wrote {len(write_all())} fixtures to {FIXTURES}")
