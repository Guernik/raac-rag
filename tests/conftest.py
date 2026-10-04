from pathlib import Path

import pytest

from raac.parser import ParsedParte, parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def parte61() -> ParsedParte:
    return parse((FIXTURES / "raac-parte-61.pdf").read_bytes())


@pytest.fixture(scope="session")
def parte1() -> ParsedParte:
    return parse((FIXTURES / "raac-parte-1.pdf").read_bytes())


@pytest.fixture(scope="session")
def parte67() -> ParsedParte:
    return parse((FIXTURES / "raac-parte-67.pdf").read_bytes())


@pytest.fixture(scope="session")
def parte91_sample() -> ParsedParte:
    # PDF pages 1, 23-25, 40, 42, 45, 48, 59 and 101 of Parte 91 IV Edición (the full PDF is 10 MB).
    return parse((FIXTURES / "raac-parte-91-sample.pdf").read_bytes())
