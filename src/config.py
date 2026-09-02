"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when config.json is missing or invalid."""


SUPPORTED_AUDIO_FORMATS = {"flac", "mp3", "opus", "aac", "wav", "wav32"}


@dataclass(frozen=True)
class Text2MusicConfig:
    """Settings used only by the no-reference text-to-music workflow."""

    music_caption: str
    audio_duration: float
    instrumental: bool
    thinking: bool
    inference_steps: int
    audio_format: str
    batch_size: int
    use_random_seed: bool
    seed: int


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
    text2music: Text2MusicConfig | None = None


def _require(mapping: dict[str, Any], key: str) -> Any:
    """Return a required JSON field or identify the missing configuration key."""
    if key not in mapping:
        raise ConfigError(f"config.json is missing required field: {key}")
    return mapping[key]


def _load_text2music(raw: dict[str, Any]) -> Text2MusicConfig | None:
    """Validate the optional text2music section without changing Remix defaults."""
    section = raw.get("text2music")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("text2music must contain a JSON object")
    try:
        config = Text2MusicConfig(
            music_caption=str(_require(section, "music_caption")).strip(),
            audio_duration=float(_require(section, "audio_duration")),
            instrumental=bool(_require(section, "instrumental")),
            thinking=bool(_require(section, "thinking")),
            inference_steps=int(_require(section, "inference_steps")),
            audio_format=str(_require(section, "audio_format")).lower(),
            batch_size=int(_require(section, "batch_size")),
            use_random_seed=bool(_require(section, "use_random_seed")),
            seed=int(section.get("seed", -1)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"text2music has an invalid value: {exc}") from exc
    if not config.music_caption or config.music_caption == "CHANGE_ME":
        raise ConfigError("Set text2music.music_caption in config.json before running.")
    if not 10 <= config.audio_duration <= 600:
        raise ConfigError("text2music.audio_duration must be between 10 and 600 seconds")
    if not 1 <= config.inference_steps:
        raise ConfigError("text2music.inference_steps must be >= 1")
    if not 1 <= config.batch_size <= 8:
        raise ConfigError("text2music.batch_size must be between 1 and 8")
    if config.audio_format not in SUPPORTED_AUDIO_FORMATS:
        raise ConfigError(f"text2music.audio_format must be one of: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}")
    if config.use_random_seed and config.seed != -1:
        raise ConfigError("text2music.seed must be -1 when text2music.use_random_seed is true")
    if not config.use_random_seed and config.seed < 0:
        raise ConfigError("text2music.seed must be a non-negative integer when random seed is disabled")
    return config


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
        text2music=_load_text2music(raw),
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
    if config.audio_format not in SUPPORTED_AUDIO_FORMATS:
        raise ConfigError(f"audio_format must be one of: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}")
    if not config.use_random_seed:
        raise ConfigError("use_random_seed must be true; actual server seeds are recorded in manifest.json")
    if config.poll_interval_seconds <= 0 or config.max_retries < 0 or config.request_timeout_seconds <= 0:
        raise ConfigError("poll_interval_seconds/request_timeout_seconds must be positive and max_retries must be >= 0")
    return config
