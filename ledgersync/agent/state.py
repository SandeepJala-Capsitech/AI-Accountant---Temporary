"""What the job's graph carries from one step to the next. LangGraph saves it at every checkpoint."""
from typing import TypedDict


class JobState(TypedDict):
    """One agent job's state. The rows themselves stay in the client's document: the state only says which
    job this is, its period, and where the fix loop has got to. Dates are ISO strings ("2025-04-01")."""

    job_id: str
    client_id: int
    period_start: str
    period_end: str
    fix_rounds: int       # fix rounds done so far
    gate_passed: bool     # whether the last quality gate passed
