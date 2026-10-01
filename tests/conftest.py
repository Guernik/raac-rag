from pathlib import Path

import pytest

from raac.parser import ParsedParte, parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def parte61() -> ParsedParte:
    return parse((FIXTURES / "raac-parte-61.pdf").read_bytes())
