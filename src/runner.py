"""Batch orchestration, recovery, concise terminal reporting and logging."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any

from .api import AceApiError, AceClient, TaskResult
from .config import AppConfig, ConfigError, load_config
from .manifest import ManifestStore


SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac"}
ILLEGAL_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_stem(stem: str) -> str:
    cleaned = ILLEGAL_WINDOWS_NAME.sub("_", stem).strip(". ")
    return cleaned or "untitled"


class BatchRemixRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config: AppConfig | None = None
        self.manifest = ManifestStore(root / "manifest.json")
        self.logger = logging.getLogger("ace_batch_remix")

    def _configure_logging(self) -> None:
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        logfile = logs / f"batch-remix-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.logger.setLevel(logging.INFO)
        for existing in self.logger.handlers:
            existing.close()
        self.logger.handlers.clear()
        handler = logging.FileHandler(logfile, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    @staticmethod
    def _line(message: str = "") -> None:
        print(message, flush=True)

    def _discover_sources(self) -> list[Path]:
        directory = self.root / "input"
        directory.mkdir(exist_ok=True)
        return sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda path: path.name.casefold(),
        )

    def _output_paths(self, source: Path, fingerprint: str, used_dirs: dict[str, str]) -> tuple[Path, list[Path]]:
        base = safe_stem(source.stem)
        chosen = base
        if chosen in used_dirs and used_dirs[chosen] != fingerprint:
            chosen = f"{base}_{fingerprint[:8]}"
        used_dirs[chosen] = fingerprint
        folder = self.root / "outputs" / chosen
        return folder, [folder / f"{chosen}_01.mp3", folder / f"{chosen}_02.mp3"]

    @staticmethod
    def _outputs_exist(record: dict[str, Any]) -> bool:
        paths = [Path(item) for item in record.get("output_paths", [])]
        return len(paths) == 2 and all(path.is_file() and path.stat().st_size > 0 for path in paths)

    def _mark_error(self, record: dict[str, Any], message: str, terminal: bool = False) -> None:
        retries = int(record.get("retry_count", 0))
        status = "failed" if terminal or retries >= self.config.max_retries else "pending"
        self.manifest.update(record, status=status, error=message, task_id=None if status == "pending" else record.get("task_id"))
        self.logger.error("%s: %s", record["source_filename"], message)

    def _submit(self, record: dict[str, Any]) -> None:
        source = Path(record["source_path"])
        try:
            task_id = self.client.submit_remix(source)
        except AceApiError as exc:
            self.manifest.update(record, retry_count=int(record.get("retry_count", 0)) + 1)
            self._mark_error(record, f"Submission failed: {exc}")
            return
        self.manifest.update(record, task_id=task_id, submitted_at=datetime.now().astimezone().isoformat(), status="queued", error=None)
        self.logger.info("Submitted %s as %s", source.name, task_id)

    def _download_completed(self, record: dict[str, Any], result: TaskResult) -> None:
        if len(result.outputs) < self.config.batch_size:
            self._mark_error(record, f"Server returned only {len(result.outputs)}/{self.config.batch_size} outputs", terminal=True)
            return
        expected = [Path(item) for item in record["output_paths"]]
        try:
            for index, output in enumerate(result.outputs[: self.config.batch_size]):
                if expected[index].is_file() and expected[index].stat().st_size > 0:
                    continue
                url = output.get("file")
                if not isinstance(url, str) or not url:
                    raise AceApiError(f"Result {index + 1} does not contain an audio file URL")
                self.client.download(url, expected[index])
        except AceApiError as exc:
            retries = int(record.get("retry_count", 0)) + 1
            if retries >= self.config.max_retries:
                self.manifest.update(record, retry_count=retries, status="failed", error=f"Download failed: {exc}")
            else:
                # Keep the completed task ID: a later query lets us retry only
                # the download instead of submitting a duplicate generation.
                self.manifest.update(record, retry_count=retries, status="download_pending", error=f"Download failed: {exc}")
            self.logger.error("%s: Download failed: %s", record["source_filename"], exc)
            return
        seeds = [str(item.get("seed_value", "")) for item in result.outputs[: self.config.batch_size]]
        self.manifest.update(record, status="completed", seeds=seeds, error=None)

    def _process_query(self, submitted: list[dict[str, Any]]) -> None:
        task_ids = [str(item["task_id"]) for item in submitted if item.get("task_id")]
        try:
            results = self.client.query(task_ids)
        except AceApiError as exc:
            self.logger.error("Batch status query failed: %s", exc)
            self._line(f"Status query temporarily failed: {exc}")
            return
        for record in submitted:
            task_id = str(record.get("task_id") or "")
            result = results.get(task_id)
            if result is None:
                self.manifest.update(record, retry_count=int(record.get("retry_count", 0)) + 1)
                self._mark_error(record, "Task is no longer present on ACE-Step server; it will be resubmitted if retries remain.")
            elif result.status == 1:
                self._download_completed(record, result)
            elif result.status == 2:
                self.manifest.update(record, retry_count=int(record.get("retry_count", 0)) + 1)
                self._mark_error(record, result.error or "ACE-Step task failed")
            else:
                self.manifest.update(record, status="running", error=None)

    def _progress(self, records: list[dict[str, Any]]) -> None:
        completed = sum(1 for record in records if self._outputs_exist(record))
        queued = sum(1 for record in records if record.get("status") == "queued")
        running = sum(1 for record in records if record.get("status") == "running")
        failed = sum(1 for record in records if record.get("status") == "failed")
        downloaded = sum(sum(1 for path in record.get("output_paths", []) if Path(path).is_file() and Path(path).stat().st_size > 0) for record in records)
        self._line(f"Completed: {completed}  Running: {running}  Queued: {queued}  Failed: {failed}  Downloaded MP3: {downloaded}")

    def _summary(self, records: list[dict[str, Any]]) -> int:
        downloaded = sum(sum(1 for path in record.get("output_paths", []) if Path(path).is_file() and Path(path).stat().st_size > 0) for record in records)
        completed = sum(1 for record in records if self._outputs_exist(record))
        failed = len(records) - completed
        self._line("\nBatch summary")
        self._line(f"Input songs:       {len(records)}")
        self._line(f"Expected outputs:  {len(records) * self.config.batch_size}")
        self._line(f"Downloaded:        {downloaded}")
        self._line(f"Completed songs:   {completed}")
        self._line(f"Failed songs:      {failed}")
        for record in records:
            missing = [str(index + 1) for index, item in enumerate(record.get("output_paths", [])) if not Path(item).is_file() or Path(item).stat().st_size == 0]
            if missing:
                self._line(f"  MISSING {record['source_filename']}: version(s) {', '.join(missing)} — {record.get('error') or record.get('status')}")
        return 0 if failed == 0 else 1

    def run(self) -> int:
        self._configure_logging()
        try:
            return self._run()
        finally:
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers.clear()

    def _run(self) -> int:
        try:
            self.config = load_config(self.root / "config.json")
            self.manifest.load()
        except (ConfigError, RuntimeError) as exc:
            self._line(f"Configuration/state error: {exc}")
            return 2
        (self.root / "outputs").mkdir(exist_ok=True)
        sources = self._discover_sources()
        if not sources:
            self._line("No supported audio found in input/. Add .mp3, .wav, or .flac files and run again.")
            return 0
        self.client = AceClient(self.config)
        self._line("ACE Batch Remix\n")
        try:
            self.client.health()
        except AceApiError as exc:
            self._line("ACE-Step API Server is unavailable.\n")
            self._line(f"Expected: {self.config.server_url}")
            self._line("\nPlease make sure:\n1. Remote ACE-Step API Server is running\n2. SSH Tunnel is connected")
            self.logger.error("Health check failed: %s", exc)
            return 2
        self._line(f"Server: ONLINE\nInput songs: {len(sources)}\nVariants/song: {self.config.batch_size}\nExpected outputs: {len(sources) * self.config.batch_size}\n")
        self._line(f"Remix Strength: {self.config.remix_strength}\nCover Strength: {self.config.cover_strength}\nFormat: MP3\n")
        used_dirs: dict[str, str] = {}
        records: list[dict[str, Any]] = []
        for source in sources:
            try:
                fingerprint = sha256_file(source)
            except OSError as exc:
                self._line(f"Cannot read {source.name}: {exc}")
                continue
            folder, paths = self._output_paths(source, fingerprint, used_dirs)
            record = self.manifest.record(fingerprint, source, folder, paths)
            if self._outputs_exist(record):
                self.manifest.update(record, status="completed", error=None)
            records.append(record)
        self.manifest.save()

        while True:
            for record in records:
                if self._outputs_exist(record) or record.get("status") == "failed":
                    continue
                if not record.get("task_id"):
                    self._submit(record)
            self.manifest.save()
            active = [record for record in records if record.get("task_id") and not self._outputs_exist(record) and record.get("status") != "failed"]
            if active:
                self._process_query(active)
                self.manifest.save()
            self._progress(records)
            if all(self._outputs_exist(record) or record.get("status") == "failed" for record in records):
                break
            time.sleep(self.config.poll_interval_seconds)
        self.manifest.save()
        return self._summary(records)
