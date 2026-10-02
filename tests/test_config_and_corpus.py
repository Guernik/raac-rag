import pytest

import os

from raac.config import load_env, load_models
from raac.corpus import CorpusError, find_listing, sheet_id_from_page


def test_models_per_stage(monkeypatch):
    for stage in ("INDEXING", "SEARCH", "ANSWER"):
        monkeypatch.delenv(f"RAAC_MODEL_{stage}", raising=False)
    m = load_models()
    assert (m.indexing, m.search, m.answer) == ("claude-haiku-4-5", "claude-opus-5-5", "claude-opus-5-5")
    monkeypatch.setenv("RAAC_MODEL_SEARCH", "claude-sonnet-5-5")
    assert load_models().search == "claude-sonnet-5-5"


def test_env_file_does_not_override_and_accepts_raac_key_name(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "RAAC_ANTHROPIC_API_KEY", "RAAC_MODEL_SEARCH", "RAAC_MODEL_ANSWER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAAC_MODEL_ANSWER", "from-env")
    env = tmp_path / ".env"
    env.write_text("RAAC_ANTHROPIC_API_KEY=sk-test\nRAAC_MODEL_SEARCH=from-file\nRAAC_MODEL_ANSWER=from-file\n")
    load_env(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["RAAC_MODEL_SEARCH"] == "from-file"
    assert os.environ["RAAC_MODEL_ANSWER"] == "from-env"
    # load_dotenv sets these outside monkeypatch; drop them so other tests see a clean env.
    for name in ("ANTHROPIC_API_KEY", "RAAC_ANTHROPIC_API_KEY", "RAAC_MODEL_SEARCH"):
        monkeypatch.delenv(name, raising=False)


def test_missing_env_file_is_fine(tmp_path):
    load_env(tmp_path / "missing.env")


def test_sheet_id_read_from_poncho_config():
    html = '<script>var ponchoTableOpciones = {"idSpread": "abc_DEF-123", "hojaNombre": "x"};</script>'
    assert sheet_id_from_page(html) == "abc_DEF-123"


def test_missing_poncho_config_fails_loudly():
    with pytest.raises(CorpusError, match="idSpread"):
        sheet_id_from_page("<html></html>")


SHEET = (
    "parte,titulo,btn-Ver,orden\n"
    "Parte,Título,Ver,Orden\n"
    'Parte 61,"**Licencias, Certificado de Competencia y Habilitaciones para piloto** ",'
    '"https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren\n",1\n'
    "Parte HL,**Helicópteros Livianos**,https://example.com/x.pdf,1\n"
)


def test_find_listing_normalizes_row():
    listing = find_listing(SHEET, "61")
    assert listing.titulo == "Licencias, Certificado de Competencia y Habilitaciones para piloto"
    assert listing.download_url == "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren/download"


def test_unknown_link_host_raises():
    with pytest.raises(CorpusError, match="unexpected link"):
        find_listing(SHEET, "HL")
