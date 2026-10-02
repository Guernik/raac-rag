"""Model per pipeline stage, read from models.toml with env overrides."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
