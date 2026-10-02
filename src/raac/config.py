"""Model per pipeline stage, read from models.toml with env overrides; local settings from .env."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_MODELS_FILE = Path(__file__).with_name("models.toml")


@dataclass(frozen=True)
class Models:
    indexing: str
    search: str
    answer: str


def load_models(path: Path = _MODELS_FILE) -> Models:
    data = tomllib.loads(path.read_text())
    stages = {}
    for stage in ("indexing", "search", "answer"):
        value = os.environ.get(f"RAAC_MODEL_{stage.upper()}") or data.get(stage)
        if not value:
            raise ValueError(f"No model configured for stage {stage!r} in {path}")
        stages[stage] = value
    return Models(**stages)


def load_env(path: Path = Path(".env")) -> None:
    """Read local settings from `path` (see .env.example) without overriding variables already set.

    The API key may also come as RAAC_ANTHROPIC_API_KEY, the name the cloud environment uses.
    """
    load_dotenv(path, override=False)
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("RAAC_ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["RAAC_ANTHROPIC_API_KEY"]
