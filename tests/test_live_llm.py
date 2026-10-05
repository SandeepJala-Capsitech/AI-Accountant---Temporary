from pathlib import Path

import pytest

from ledgersync.config import Settings
from ledgersync.extractor import TransactionExtractor
from ledgersync.groq_client import GroqClient
from ledgersync.pipeline import prepare_image

pytestmark = pytest.mark.llm
FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_groq_reads_a_receipt_photo():
    settings = Settings.from_env()
    if not settings.groq_api_key:
        pytest.skip("set GROQ_API_KEY (e.g. in .env) to run")
    client = GroqClient(settings)
    client.ensure_available()
    photo = (FIXTURES / "img-train-ticket" / "input.png").read_bytes()
    result = TransactionExtractor(client).extract_accounting_data(images=[prepare_image(photo)])
    assert any(abs(abs(t.amount) - 87.50) < 0.01 for t in result.data)
