"""Paths plus the scenario and model configuration files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPENTTD_DIR = ROOT / "third_party" / "openttd"
OPENTTD_BIN = OPENTTD_DIR / "openttd"
SCRIPTS_DIR = ROOT / "ottd_scripts"
SCENARIOS_DIR = ROOT / "scenarios"
RUNS_DIR = ROOT / "runs"
MODELS_FILE = ROOT / "models.yaml"


@dataclass
class GameConfig:
    seed: int = 1
    map_size: int = 64          # tiles per side; a power of two from 64 to 512
    start_year: int = 1990
    climate: str = "temperate"
    towns: int = 2              # 0 very low .. 3 high
    industries: int = 4         # 1 minimal .. 5 high
    terrain: int = 0            # 0 very flat .. 3 mountainous
    sea: int = 0                # 0 very low .. 3 high
    rivers: int = 0
    max_loan: int = 300000
    breakdowns: int = 0         # 0 none, 1 reduced, 2 normal
    extra_settings: dict = field(default_factory=dict)   # "section.key": value, written verbatim


@dataclass
class EpisodeConfig:
    max_steps: int = 60          # model turns
    days_per_step: int = 4       # game days that pass after every model turn
    max_days: int = 365          # episode ends when this many game days have passed
    run_out_clock: bool = True   # when turns run out early, let the remaining days pass before scoring
    max_wait_days: int = 60      # upper bound for one wait() call
    max_tool_calls_per_step: int = 6
    max_images_per_step: int = 2
    # A reply with no tool call does nothing. With this on, the model is reminded once, within the
    # same turn, and may then act; turns that stay empty are still counted as idle.
    nudge_idle_turns: bool = True
    # When a stop or depot cannot be built, name nearby tiles that would work. Turn off to make
    # the model find sites from the screenshots alone.
    spatial_hints: bool = True


@dataclass
class Scenario:
    name: str
    description: str = ""
    goal: str = ""
    transport: list = field(default_factory=lambda: ["road"])
    game: GameConfig = field(default_factory=GameConfig)
    episode: EpisodeConfig = field(default_factory=EpisodeConfig)

    @staticmethod
    def load(ref: str) -> "Scenario":
        path = Path(ref)
        if not path.exists():
            path = SCENARIOS_DIR / f"{ref}.yaml"
        if not path.exists():
            known = ", ".join(sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml")))
            raise FileNotFoundError(f"scenario '{ref}' not found. Known scenarios: {known}")
        raw = yaml.safe_load(path.read_text())
        size = raw.get("game", {}).get("map_size", 64)
        if size not in (64, 128, 256, 512):
            raise ValueError(f"{path}: game.map_size must be 64, 128, 256 or 512")
        return Scenario(
            name=raw.get("name", path.stem),
            description=raw.get("description", ""),
            goal=raw.get("goal", "").strip(),
            transport=raw.get("transport", ["road"]),
            game=GameConfig(**raw.get("game", {})),
            episode=EpisodeConfig(**raw.get("episode", {})),
        )


@dataclass
class ModelConfig:
    key: str
    provider: str                # ollama | openai | anthropic
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    tool_mode: str = "native"    # native | json (for models without tool calling)
    temperature: float | None = None
    max_tokens: int = 2048
    context_tokens: int = 32000  # history budget; the agent compacts when it is exceeded
    image_max_side: int = 1024
    options: dict = field(default_factory=dict)   # provider-specific extras

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    @staticmethod
    def load(key: str, path: Path = MODELS_FILE) -> "ModelConfig":
        models = yaml.safe_load(path.read_text())["models"]
        if key not in models:
            raise ValueError(f"model '{key}' is not in {path.name}. Known models: {', '.join(sorted(models))}")
        return ModelConfig(key=key, **models[key])

    @staticmethod
    def all_keys(path: Path = MODELS_FILE) -> list[str]:
        return sorted(yaml.safe_load(path.read_text())["models"])
