"""Asks the model for a document's transactions: the prompt, the JSON schema its answer must follow,
and per-row validation."""
import json
import logging
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .accounts import choosable
from .errors import ModelError

logger = logging.getLogger(__name__)

# The accounts a model may choose, as "<code> <name>", e.g. "7502 Telephone and Internet".
ACCOUNT_CHOICES: tuple[str, ...] = tuple(f"{a.code} {a.name}" for a in choosable())


class AccountingTransaction(BaseModel):
    """One row as the model returns it; its JSON schema holds the model's answer to this shape."""
    # The docstring above is sent to the model in the schema. groq_client.strict_schema makes every
    # field required, so structured output always emits it (null when unknown).

    description: str = Field(..., description="Who was paid or who paid, and what for")
    date: Optional[str] = Field(None, description="YYYY-MM-DD, or null when the document shows no date")
    amount: float = Field(..., description="The money that moved, including VAT, as a positive number")
    direction: Literal["in", "out"] = Field(
        ..., description="in: money into the business's bank account; out: money paid out of it")
    # The schema lists the chart so the model picks from it; any other text still passes here, and the
    # ledger adapter sends it to Suspense for review instead of dropping the row.
    account: str = Field(..., description="The account from the chart of accounts",
                         json_schema_extra={"enum": list(ACCOUNT_CHOICES)})
    vat: Optional[float] = Field(None, description="The VAT amount printed on the document, or null")
    currency: Optional[str] = Field("GBP", description="Currency code, e.g. GBP")
    # Per receipt or invoice: lets the ledger flag rows that do not add up to the document's total,
    # and a document kept as one row because its VAT could not be divided between accounts.
    document_total: Optional[float] = Field(
        None, description="The printed total of the receipt or invoice this transaction comes from, the same "
                          "on every transaction from that document; null for bank statement or spreadsheet rows")
    document_vat: Optional[float] = Field(
        None, description="The total VAT printed on that receipt or invoice, the same on every transaction from "
                          "it; null when it shows no VAT, and for bank statement or spreadsheet rows")
    mixed_items: bool = Field(False, description="true when this one transaction covers items of different "
                                                 "accounts because the document shows one VAT total that "
                                                 "cannot be divided between them")

    @field_validator("mixed_items", mode="before")
    @classmethod
    def _mixed_items_flag(cls, v):
        return v.strip().lower() == "true" if isinstance(v, str) else bool(v)

    @field_validator("direction", mode="before")
    @classmethod
    def _lowercase_direction(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("account", mode="before")
    @classmethod
    def _account_text(cls, v):
        return "" if v is None else str(v)


class ExtractedTransactions(BaseModel):
    """Top-level shape the model must return; its JSON schema goes to the model client."""
    transactions: List[AccountingTransaction]


class TransactionExtractionResult(BaseModel):
    data: List[AccountingTransaction]
    warnings: List[str] = []
    model: Optional[str] = None

    @property
    def count(self) -> int:
        return len(self.data)


class TransactionExtractor:
    """Builds the prompt, calls the model through the Groq client and validates each row.

    There is deliberately no heuristic fallback: when the model is unavailable the caller gets
    ModelUnavailable / ModelTimeout instead of plausible-looking wrong numbers."""

    def __init__(self, client, business_name: str = ""):
        self.client = client
        self.business_name = business_name

    def extract_accounting_data(self, text_input: Optional[str] = None,
                                images: Optional[List[str]] = None) -> TransactionExtractionResult:
        messages = [{"role": "system", "content": self._instructions()},
                    {"role": "user", "content": self._document(text_input, images)}]
        raw_output = self.client.chat_json(messages, ExtractedTransactions.model_json_schema(), images=images)
        return self._validate_and_parse_json(raw_output)

    def _instructions(self) -> str:
        """What to extract and how; the document itself travels in a separate message."""
        name = self.business_name
        whose = f"{name}, a UK business" if name else "a UK business"
        invoices = (f" An invoice issued by {name} is a sale (in); an invoice or receipt addressed to {name} "
                    "is a purchase (out)." if name else "")
        chart = "\n".join(f"{a.code} {a.name}: {a.definition}" for a in choosable())
        return f"""You keep the books of {whose}. List each movement of money into or out of the business's bank account that the document records, as a JSON object with a "transactions" array.

Rules:
1. Direction: "in" is money the business received (sales, refunds from suppliers, loans received, capital paid in); "out" is money it paid (purchases, expenses, bills, wages, taxes to HMRC, refunds to customers, the owner's drawings, transfers to savings).{invoices}
2. A receipt or an invoice is recorded whether it is still to be paid or already paid (an amount due of 0.00 means it has been paid, not that there is nothing to record). Give one transaction per account, not per item: add together the items that belong to the same account, so a receipt or invoice whose items all belong to one account is ONE transaction for its total, including VAT. When its items belong to different accounts, give one transaction per account for that account's share of the total, including its VAT, but only if the VAT can be divided between them (it is shown per item or per rate, or no VAT is shown). If the document shows a single VAT total that cannot be divided, give ONE transaction for the total on the account of the biggest items and set mixed_items to true. The transactions from one document add up to its total: give that printed total as document_total, and its printed VAT total as document_vat (null when none is shown), on each of them. Subtotals, discounts, cash tendered, change, card-payment, amount-paid and amount-due lines, and payment instructions, are not transactions.
3. A bank statement or spreadsheet has one transaction per payment row. The amount is the money that moved, as a positive number; take the direction from the paid in / paid out columns or the sign. Balances and totals are not transactions, and document_total and document_vat are null.
4. Account: choose by what was bought and why, not by the shop, because most shops sell many kinds of things. On a receipt or invoice, go by the items listed. On a bank line that names only the payee, go by what that kind of business usually sells to a business like this one. Use "9998 Suspense" when the payee could be selling almost anything (such as an online marketplace or a department store) and nothing says what was bought, or when no account below fits.
5. VAT: the VAT amount printed on the document for that transaction, or null when none is printed. Never calculate VAT.
6. Dates are UK format (DD/MM/YYYY): "03/09/2026" is 3 September 2026, written "2026-09-03". Use null when there is no date.
7. If there are no transactions, return {{"transactions": []}}.

Chart of accounts:
{chart}

Answer with JSON only, in this shape (one object per transaction):
{{"transactions": [{{"description": "who was paid or who paid, and what for", "date": "YYYY-MM-DD" or null, "amount": 12.50, "direction": "in" or "out", "account": "<code> <name> from the chart, e.g. 7502 Telephone and Internet", "vat": 2.08 or null, "currency": "GBP", "document_total": 12.50 or null, "document_vat": 2.08 or null, "mixed_items": false}}]}}"""

    @staticmethod
    def _document(text_input: Optional[str], images: Optional[List[str]]) -> str:
        if images:
            return f"The document is attached as {len(images)} image(s)."
        return f"<document>\n{text_input or ''}\n</document>"

    def _validate_and_parse_json(self, raw_text: str) -> TransactionExtractionResult:
        """Parses the model output. Rows that fail validation become warnings, not a failed batch."""
        cleaned = raw_text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            raise ModelError("The AI model's answer was not valid JSON.", code="ai_output_invalid") from None
        rows = parsed.get("transactions") if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            raise ModelError("The AI model's answer did not contain a list of transactions.",
                             code="ai_output_invalid")

        valid, warnings = [], []
        for number, row in enumerate(rows, start=1):
            try:
                valid.append(AccountingTransaction.model_validate(row))
            except ValidationError as exc:
                reasons = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}"
                                    for e in exc.errors())
                warnings.append(f"Row {number} skipped ({reasons}).")
        if warnings:
            logger.info("Skipped %d invalid row(s) from the model", len(warnings))
        return TransactionExtractionResult(data=valid, warnings=warnings, model=self.client.model)
