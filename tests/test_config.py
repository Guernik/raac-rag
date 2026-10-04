import os

from raac.config import load_env, load_models


def test_models_per_stage(monkeypatch):
    for stage in ("INDEXING", "ROUTING", "SEARCH", "ANSWER"):
        monkeypatch.delenv(f"RAAC_MODEL_{stage}", raising=False)
    m = load_models()
    assert (m.indexing, m.routing, m.search, m.answer) == (
        "claude-haiku-4-5",
        "claude-haiku-4-5",
        "claude-opus-5-5",
        "claude-opus-5-5",
    )
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
