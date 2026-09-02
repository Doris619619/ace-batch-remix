"""All ACE-Step HTTP details, including the single Remix payload mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .config import AppConfig, Text2MusicConfig


class AceApiError(RuntimeError):
    """A request reached ACE-Step but could not be used safely."""


class AceSubmissionUncertain(AceApiError):
    """The submit request timed out after it may already have reached ACE-Step."""


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: int
    outputs: list[dict[str, Any]]
    error: str | None = None


def build_remix_payload(config: AppConfig) -> dict[str, str]:
    """Map this project's Remix controls to the current ACE-Step REST fields.

    The local UI term "Remix" maps to ACE-Step's full-audio ``cover`` task.
    Keeping the mapping here makes future ACE-Step API adjustments isolated.
    """
    return {
        "task_type": "cover",
        "prompt": config.music_caption,
        "lyrics": "",
        "audio_cover_strength": str(config.remix_strength),
        "cover_noise_strength": str(config.cover_strength),
        "batch_size": str(config.batch_size),
        "audio_format": config.audio_format,
        "use_random_seed": "true",
    }


def build_text2music_payload(config: Text2MusicConfig, batch_size: int, seed: int) -> dict[str, object]:
    """Build the JSON-only ACE-Step text-to-music request.

    ``[Instrumental]`` is ACE-Step's control token for an instrumental result;
    it is deliberately sent alongside the explicit boolean so both current and
    older server revisions receive an unambiguous no-vocals instruction.
    """
    return {
        "task_type": "text2music",
        "prompt": config.music_caption,
        "lyrics": "[Instrumental]" if config.instrumental else "",
        "instrumental": config.instrumental,
        "audio_duration": config.audio_duration,
        "thinking": config.thinking,
        "inference_steps": config.inference_steps,
        "audio_format": config.audio_format,
        "batch_size": batch_size,
        "use_random_seed": config.use_random_seed,
        "seed": seed,
    }


class AceClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.base_url = config.server_url + "/"

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request_with_backoff(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Retry only idempotent reads/queries; never replay task submission."""
        last_error: requests.RequestException | None = None
        for attempt in range(3):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code < 500 or attempt == 2:
                    return response
                response.close()
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 2:
                    raise
            time.sleep(0.5 * (2**attempt))
        raise AceApiError(str(last_error or "HTTP request failed"))

    @staticmethod
    def _unwrap(response: requests.Response) -> Any:
        try:
            body = response.json()
        except ValueError as exc:
            raise AceApiError(f"ACE-Step returned non-JSON response: {response.text[:300]}") from exc
        if not isinstance(body, dict):
            raise AceApiError("ACE-Step returned an invalid response wrapper")
        if not response.ok or body.get("code", 200) != 200 or body.get("error"):
            raise AceApiError(f"ACE-Step API error: {body.get('error') or response.text[:300]}")
        return body.get("data")

    def health(self) -> None:
        try:
            response = self._request_with_backoff("GET", self._url("/health"), timeout=self.config.request_timeout_seconds)
            self._unwrap(response)
        except requests.RequestException as exc:
            raise AceApiError(str(exc)) from exc

    def submit_remix(self, source: Path) -> str:
        try:
            with source.open("rb") as handle:
                response = self.session.post(
                    self._url("/release_task"),
                    data=build_remix_payload(self.config),
                    files={"src_audio": (source.name, handle, "application/octet-stream")},
                    timeout=self.config.request_timeout_seconds,
                )
            data = self._unwrap(response)
        except requests.ReadTimeout as exc:
            raise AceSubmissionUncertain(
                "Submission timed out before a task_id was returned. The server may still have accepted it; "
                "the client will not automatically resubmit this source."
            ) from exc
        except (OSError, requests.RequestException) as exc:
            raise AceApiError(str(exc)) from exc
        if not isinstance(data, dict) or not data.get("task_id"):
            raise AceApiError("ACE-Step response did not include a task_id")
        return str(data["task_id"])

    def submit_text2music(self, config: Text2MusicConfig, batch_size: int, seed: int) -> str:
        """Submit a text-only task exactly once; unknown outcomes stay manual."""
        try:
            response = self.session.post(
                self._url("/release_task"),
                json=build_text2music_payload(config, batch_size, seed),
                timeout=self.config.request_timeout_seconds,
            )
            data = self._unwrap(response)
        except requests.ReadTimeout as exc:
            raise AceSubmissionUncertain(
                "Submission timed out before a task_id was returned. The server may still have accepted it; "
                "the client will not automatically resubmit this text2music batch."
            ) from exc
        except requests.RequestException as exc:
            raise AceApiError(str(exc)) from exc
        if not isinstance(data, dict) or not data.get("task_id"):
            raise AceApiError("ACE-Step response did not include a task_id")
        return str(data["task_id"])

    def query(self, task_ids: list[str]) -> dict[str, TaskResult]:
        if not task_ids:
            return {}
        try:
            response = self._request_with_backoff(
                "POST",
                self._url("/query_result"),
                json={"task_id_list": task_ids},
                timeout=self.config.request_timeout_seconds,
            )
            data = self._unwrap(response)
        except requests.RequestException as exc:
            raise AceApiError(str(exc)) from exc
        if not isinstance(data, list):
            raise AceApiError("ACE-Step query_result response was not a list")
        results: dict[str, TaskResult] = {}
        for item in data:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            raw_result = item.get("result", [])
            if isinstance(raw_result, str):
                import json

                try:
                    raw_result = json.loads(raw_result)
                except json.JSONDecodeError:
                    raw_result = []
            outputs = raw_result if isinstance(raw_result, list) else []
            results[str(item["task_id"])] = TaskResult(
                task_id=str(item["task_id"]),
                status=int(item.get("status", 0)),
                outputs=[entry for entry in outputs if isinstance(entry, dict)],
                error=str(item.get("error")) if item.get("error") else None,
            )
        return results

    def download(self, relative_or_absolute_url: str, destination: Path) -> None:
        url = urljoin(self.base_url, relative_or_absolute_url)
        temporary = destination.with_name(destination.name + ".part")
        try:
            temporary.unlink(missing_ok=True)
            with self._request_with_backoff("GET", url, stream=True, timeout=self.config.request_timeout_seconds) as response:
                if not response.ok:
                    raise AceApiError(f"Audio download failed with HTTP {response.status_code}: {response.text[:300]}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with temporary.open("wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            target.write(chunk)
                            written += len(chunk)
            if written == 0:
                raise AceApiError("Audio download was empty")
            temporary.replace(destination)
        except (OSError, requests.RequestException) as exc:
            raise AceApiError(str(exc)) from exc
        finally:
            try:
                if temporary.exists():
                    temporary.unlink(missing_ok=True)
            except OSError:
                # A concurrent recovery process may still own the transient
                # file. The next manifest-driven retry will handle it safely.
                pass
