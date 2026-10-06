"""Model per pipeline stage (and for the eval judge), read from models.toml with env overrides; local settings from .env."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_MODELS_FILE = Path(__file__).with_name("models.toml")


@dataclass(frozen=True)
class Models:
    indexing: str
    routing: str
    search: str
    answer: str


def load_models(path: Path = _MODELS_FILE) -> Models:
    data = tomllib.loads(path.read_text())
    stages = {}
    for stage in ("indexing", "routing", "search", "answer"):
        value = os.environ.get(f"RAAC_MODEL_{stage.upper()}") or data.get(stage)
        if not value:
            raise ValueError(f"No model configured for stage {stage!r} in {path}")
        stages[stage] = value
    return Models(**stages)


def load_judge_model(path: Path = _MODELS_FILE) -> str:
    """Model for the eval correctness judge; not a pipeline stage, so not part of Models."""
    value = os.environ.get("RAAC_MODEL_JUDGE") or tomllib.loads(path.read_text()).get("judge")
    if not value:
        raise ValueError(f"No model configured for the eval judge in {path}")
    return value


def load_env(path: Path = Path(".env")) -> None:
    """Read local settings from `path` (see .env.example) without overriding variables already set.

    The API key may also come as RAAC_ANTHROPIC_API_KEY, the name the cloud environment uses.
    """
    load_dotenv(path, override=False)
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("RAAC_ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["RAAC_ANTHROPIC_API_KEY"]
