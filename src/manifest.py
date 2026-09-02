"""Durable, atomically-written local run state."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ManifestStore:
    """Maintains one record per source file fingerprint."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": 2, "songs": {}, "text2music_runs": {}}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read manifest.json: {exc}") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("songs"), dict):
            raise RuntimeError("manifest.json has an unsupported format")
        # Version 1 stores only Remix records. Keep it intact and add the new
        # namespace lazily so an interrupted legacy Remix run remains usable.
        if not isinstance(loaded.get("text2music_runs", {}), dict):
            raise RuntimeError("manifest.json has an unsupported text2music_runs format")
        loaded.setdefault("text2music_runs", {})
        loaded["version"] = max(int(loaded.get("version", 1)), 2)
        self.data = loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def record(
        self,
        run_source_fingerprint: str,
        source_fingerprint: str,
        source_path: Path,
        output_dir: Path,
        output_paths: list[Path],
        run_fingerprint: str,
    ) -> dict[str, Any]:
        """Create or retrieve one source record scoped to immutable generation settings."""
        songs: dict[str, dict[str, Any]] = self.data["songs"]
        if run_source_fingerprint not in songs:
            songs[run_source_fingerprint] = {
                "source_filename": source_path.name,
                "source_path": str(source_path.resolve()),
                "source_fingerprint_sha256": source_fingerprint,
                "run_fingerprint": run_fingerprint,
                "output_dir": str(output_dir.resolve()),
                "output_paths": [str(item.resolve()) for item in output_paths],
                "task_id": None,
                "submitted_at": None,
                "status": "pending",
                "retry_count": 0,
                "seeds": [],
                "error": None,
                "updated_at": now_iso(),
            }
        return songs[run_source_fingerprint]

    def text2music_run(
        self,
        run_fingerprint: str,
        request: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        """Create or retrieve durable state for one settings-scoped text run."""
        runs: dict[str, dict[str, Any]] = self.data["text2music_runs"]
        if run_fingerprint not in runs:
            runs[run_fingerprint] = {
                "run_fingerprint": run_fingerprint,
                "request": request,
                "output_dir": str(output_dir.resolve()),
                "tracks": {},
                "tasks": {},
                "next_task_number": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        return runs[run_fingerprint]

    @staticmethod
    def update(record: dict[str, Any], **fields: Any) -> None:
        record.update(fields)
        record["updated_at"] = now_iso()

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data)
