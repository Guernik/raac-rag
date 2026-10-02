from raac.config import load_models


def test_models_per_stage(monkeypatch):
    for stage in ("INDEXING", "SEARCH", "ANSWER"):
        monkeypatch.delenv(f"RAAC_MODEL_{stage}", raising=False)
    m = load_models()
    assert (m.indexing, m.search, m.answer) == ("claude-haiku-4-5", "claude-opus-5-5", "claude-opus-5-5")
    monkeypatch.setenv("RAAC_MODEL_SEARCH", "claude-sonnet-5-5")
    assert load_models().search == "claude-sonnet-5-5"

