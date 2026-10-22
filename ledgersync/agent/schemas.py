"""What an agent returns when it finishes. The model returns it by calling a tool named after the class, whose
description is the class's docstring, so the docstring and the field descriptions are written for the model."""
from pydantic import BaseModel, Field


class PostingResult(BaseModel):
    """Call this once, when you have worked through every row that needs a look, to say what you did."""

    rows_changed: int = Field(ge=0, description="How many rows you changed with update_row.")
    exceptions_logged: int = Field(ge=0, description="How many exceptions you logged with log_exception.")
    requests_made: int = Field(ge=0, description="How many documents you asked the client for.")
    summary: str = Field(min_length=1, max_length=500,
                         description="One or two plain sentences for the accountant, such as "
                                     "\"Coded 3 rows; 1 left for review.\"")
