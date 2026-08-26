"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when config.json is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    server_url: str
    music_caption: str
    generation_mode: str
    remix_strength: float
    cover_strength: float
    batch_size: int
    audio_format: str
    use_random_seed: bool
    poll_interval_seconds: float
    max_retries: int
    request_timeout_seconds: float


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"config.json is missing required field: {key}")
    return mapping[key]


def load_config(path: Path, *, allow_placeholder_caption: bool = False) -> AppConfig:
    """Load a valid user configuration, allowing batch sizes supported by ACE-Step."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object")

    config = AppConfig(
        server_url=str(_require(raw, "server_url")).rstrip("/"),
        music_caption=str(_require(raw, "music_caption")).strip(),
        generation_mode=str(_require(raw, "generation_mode")).lower(),
        remix_strength=float(_require(raw, "remix_strength")),
        cover_strength=float(_require(raw, "cover_strength")),
        batch_size=int(_require(raw, "batch_size")),
        audio_format=str(_require(raw, "audio_format")).lower(),
        use_random_seed=bool(_require(raw, "use_random_seed")),
        poll_interval_seconds=float(_require(raw, "poll_interval_seconds")),
        max_retries=int(_require(raw, "max_retries")),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 60)),
    )
    if not config.server_url.startswith(("http://", "https://")):
        raise ConfigError("server_url must start with http:// or https://")
    if not config.music_caption or (config.music_caption == "CHANGE_ME" and not allow_placeholder_caption):
        raise ConfigError("Set music_caption in config.json before running.")
    if config.generation_mode != "remix":
        raise ConfigError("generation_mode must be 'remix'.")
    for name, value in (("remix_strength", config.remix_strength), ("cover_strength", config.cover_strength)):
        if not 0 <= value <= 1:
            raise ConfigError(f"{name} must be between 0.0 and 1.0")
    if not 1 <= config.batch_size <= 8:
        raise ConfigError("batch_size must be between 1 and 8 for the configured ACE-Step server")
    if config.audio_format != "mp3":
        raise ConfigError("audio_format must be 'mp3' for this experiment")
    if not config.use_random_seed:
        raise ConfigError("use_random_seed must be true; actual server seeds are recorded in manifest.json")
    if config.poll_interval_seconds <= 0 or config.max_retries < 0 or config.request_timeout_seconds <= 0:
        raise ConfigError("poll_interval_seconds/request_timeout_seconds must be positive and max_retries must be >= 0")
    return config
