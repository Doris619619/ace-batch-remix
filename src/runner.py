"""Batch orchestration, recovery, concise terminal reporting and logging."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

from .api import AceApiError, AceClient, AceSubmissionUncertain, TaskResult
from .config import AppConfig, ConfigError, Text2MusicConfig, load_config
from .manifest import ManifestStore


SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac"}
ILLEGAL_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FORMAT_SUFFIX = {"flac": ".flac", "mp3": ".mp3", "opus": ".opus", "aac": ".aac", "wav": ".wav", "wav32": ".wav"}


def sha256_file(path: Path) -> str:
    """Return the content hash used to keep distinct Remix source files separate."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_stem(stem: str) -> str:
    """Normalize a user label or source name into a valid Windows filename stem."""
    cleaned = ILLEGAL_WINDOWS_NAME.sub("_", stem).strip(". ")
    return cleaned or "untitled"


def remix_run_fingerprint(config: AppConfig) -> str:
    """Identify outputs generated with the same source-independent Remix settings."""
    payload = {
        "music_caption": config.music_caption,
        "generation_mode": config.generation_mode,
        "remix_strength": config.remix_strength,
        "cover_strength": config.cover_strength,
        "batch_size": config.batch_size,
        "audio_format": config.audio_format,
        "use_random_seed": config.use_random_seed,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def text2music_run_fingerprint(config: Text2MusicConfig, label: str | None = None) -> str:
    """Identity for a resumable collection; count and client chunk size are excluded."""
    payload = asdict(config)
    payload.pop("batch_size")
    if label is not None:
        payload["output_label"] = label
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def audio_suffix(audio_format: str) -> str:
    """Map ACE-Step's format value to the local output filename extension."""
    return FORMAT_SUFFIX[audio_format]


class BatchRemixRunner:
    def __init__(self, root: Path, *, limit: int | None = None, selected_file: str | None = None,
                 caption_override: str | None = None, mode: str = "remix", count: int | None = None,
                 text_label: str | None = None) -> None:
        """Keep CLI choices and durable state for one Remix or text2music run."""
        self.root = root
        self.limit = limit
        self.selected_file = selected_file
        self.caption_override = caption_override
        self.mode = mode
        self.count = count
        self.text_label = text_label
        self.config: AppConfig | None = None
        self.manifest = ManifestStore(root / "manifest.json")
        self.logger = logging.getLogger("ace_batch_remix")

    def _configure_logging(self) -> None:
        """Create a mode-specific log file and replace handlers from prior runs."""
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        logfile = logs / f"batch-{self.mode}-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.logger.setLevel(logging.INFO)
        for existing in self.logger.handlers:
            existing.close()
        self.logger.handlers.clear()
        handler = logging.FileHandler(logfile, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    @staticmethod
    def _line(message: str = "") -> None:
        """Print an immediately flushed terminal status line for interactive use."""
        print(message, flush=True)

    def _discover_sources(self) -> list[Path]:
        """Select valid Remix sources only; text2music never calls this method."""
        directory = self.root / "input"
        directory.mkdir(exist_ok=True)
        sources = sorted((path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS), key=lambda path: path.name.casefold())
        if self.selected_file:
            candidate = Path(self.selected_file)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            candidate = candidate.resolve()
            if directory.resolve() not in candidate.parents or not candidate.is_file():
                raise ConfigError("--file must name an existing audio file under input/.")
            if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise ConfigError("--file must be .mp3, .wav, or .flac")
            return [candidate]
        if self.limit is not None:
            if self.limit <= 0:
                raise ConfigError("--limit must be a positive integer")
            return sources[:self.limit]
        return sources

    def _output_paths(self, source: Path, source_fingerprint: str, run_fingerprint: str,
                      used_dirs: dict[str, str]) -> tuple[Path, list[Path]]:
        """Allocate non-overwriting Remix paths with the configured audio extension."""
        base = safe_stem(source.stem)
        chosen = base if base not in used_dirs or used_dirs[base] == source_fingerprint else f"{base}_{source_fingerprint[:8]}"
        used_dirs[chosen] = source_fingerprint
        run_name = f"{chosen}__{run_fingerprint[:8]}"
        folder = self.root / "outputs" / run_name
        suffix = audio_suffix(self.config.audio_format)
        return folder, [folder / f"{run_name}_{index:02d}{suffix}" for index in range(1, self.config.batch_size + 1)]

    def _outputs_exist(self, record: dict[str, Any]) -> bool:
        """Return true only when every expected Remix output is non-empty on disk."""
        paths = [Path(item) for item in record.get("output_paths", [])]
        return len(paths) == self.config.batch_size and all(path.is_file() and path.stat().st_size > 0 for path in paths)

    def _mark_error(self, record: dict[str, Any], message: str, terminal: bool = False) -> None:
        retries = int(record.get("retry_count", 0))
        status = "failed" if terminal or retries >= self.config.max_retries else "pending"
        self.manifest.update(record, status=status, error=message, task_id=None if status == "pending" else record.get("task_id"))
        self.logger.error("%s: %s", record["source_filename"], message)

    def _submit(self, record: dict[str, Any]) -> None:
        source = Path(record["source_path"])
        try:
            task_id = self.client.submit_remix(source)
        except AceSubmissionUncertain as exc:
            self.manifest.update(record, status="submission_uncertain", error=str(exc))
            self.logger.error("%s: %s", source.name, exc)
            self._line(f"Submission needs manual confirmation for {source.name}; it was not automatically retried.")
            return
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
            for index, output in enumerate(result.outputs[:self.config.batch_size]):
                if expected[index].is_file() and expected[index].stat().st_size > 0:
                    continue
                url = output.get("file")
                if not isinstance(url, str) or not url:
                    raise AceApiError(f"Result {index + 1} does not contain an audio file URL")
                self.client.download(url, expected[index])
        except AceApiError as exc:
            retries = int(record.get("retry_count", 0)) + 1
            status = "failed" if retries >= self.config.max_retries else "download_pending"
            self.manifest.update(record, retry_count=retries, status=status, error=f"Download failed: {exc}")
            self.logger.error("%s: Download failed: %s", record["source_filename"], exc)
            return
        self.manifest.update(record, status="completed", seeds=[str(item.get("seed_value", "")) for item in result.outputs[:self.config.batch_size]], error=None)

    def _process_query(self, submitted: list[dict[str, Any]]) -> None:
        try:
            results = self.client.query([str(item["task_id"]) for item in submitted if item.get("task_id")])
        except AceApiError as exc:
            self.logger.error("Batch status query failed: %s", exc)
            self._line(f"Status query temporarily failed: {exc}")
            return
        for record in submitted:
            result = results.get(str(record.get("task_id") or ""))
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
        self._line(f"Completed: {completed}  Running: {running}  Queued: {queued}  Failed: {failed}  Downloaded {self.config.audio_format.upper()}: {downloaded}")

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

    # ---- text2music workflow -------------------------------------------------

    @staticmethod
    def _track_completed(track: dict[str, Any]) -> bool:
        """Treat a track as complete only after its final non-empty file exists."""
        path = Path(track["output_path"])
        return path.is_file() and path.stat().st_size > 0

    def _ensure_text_tracks(self, run: dict[str, Any], count: int, config: Text2MusicConfig) -> list[dict[str, Any]]:
        """Create stable ordinals through count and repair stale completed statuses."""
        tracks: dict[str, dict[str, Any]] = run["tracks"]
        output_dir = Path(run["output_dir"])
        prefix = f"{run['label']}_" if run.get("label") else "track_"
        for ordinal in range(1, count + 1):
            key = f"{ordinal:04d}"
            if key not in tracks:
                tracks[key] = {"ordinal": ordinal, "output_path": str((output_dir / f"{prefix}{ordinal:04d}{audio_suffix(config.audio_format)}").resolve()),
                               "status": "pending", "task_key": None, "result_index": None, "seed": None,
                               "retry_count": 0, "error": None, "updated_at": datetime.now().astimezone().isoformat()}
            elif self._track_completed(tracks[key]):
                self.manifest.update(tracks[key], status="completed", error=None)
            elif tracks[key].get("status") == "completed":
                self.manifest.update(tracks[key], status="pending", task_key=None, result_index=None)
        return [tracks[f"{ordinal:04d}"] for ordinal in range(1, count + 1)]

    def _new_text_task(self, run: dict[str, Any], tracks: list[dict[str, Any]], config: Text2MusicConfig) -> dict[str, Any]:
        """Persist one API task and derive a deterministic seed from its first track."""
        task_number = int(run.get("next_task_number", 1))
        task_key = f"task-{task_number:04d}"
        run["next_task_number"] = task_number + 1
        seed = -1 if config.use_random_seed else config.seed + int(tracks[0]["ordinal"]) - 1
        task = {"task_key": task_key, "track_keys": [f"{int(track['ordinal']):04d}" for track in tracks],
                "batch_size": len(tracks), "seed": seed, "task_id": None, "submitted_at": None,
                "status": "pending", "retry_count": 0, "error": None, "updated_at": datetime.now().astimezone().isoformat()}
        run["tasks"][task_key] = task
        for index, track in enumerate(tracks):
            self.manifest.update(track, status="pending", task_key=task_key, result_index=index, error=None)
        self.manifest.update(run)
        return task

    def _release_pending_text_tracks(self, run: dict[str, Any], selected: list[dict[str, Any]], config: Text2MusicConfig) -> None:
        """Group unresolved, unassigned tracks into API tasks no larger than batch_size."""
        pending = [track for track in selected if not self._track_completed(track) and track.get("status") == "pending" and not track.get("task_key")]
        while pending:
            self._new_text_task(run, pending[:config.batch_size], config)
            pending = pending[config.batch_size:]

    def _submit_text_task(self, run: dict[str, Any], task: dict[str, Any], config: Text2MusicConfig) -> None:
        """Submit one persisted text task without replaying timeout-uncertain requests."""
        tracks = run["tracks"]
        try:
            task_id = self.client.submit_text2music(config, int(task["batch_size"]), int(task["seed"]))
        except AceSubmissionUncertain as exc:
            self.manifest.update(task, status="submission_uncertain", error=str(exc))
            for key in task["track_keys"]:
                if not self._track_completed(tracks[key]):
                    self.manifest.update(tracks[key], status="submission_uncertain", error=str(exc))
            self.logger.error("%s: %s", task["task_key"], exc)
            self._line(f"Submission needs manual confirmation for text2music {task['task_key']}; it was not automatically retried.")
            return
        except AceApiError as exc:
            self._retry_or_fail_text_task(run, task, f"Submission failed: {exc}")
            return
        self.manifest.update(task, task_id=task_id, submitted_at=datetime.now().astimezone().isoformat(), status="queued", error=None)
        for key in task["track_keys"]:
            if not self._track_completed(tracks[key]):
                self.manifest.update(tracks[key], status="queued", error=None)
        self.logger.info("Submitted %s as %s", task["task_key"], task_id)

    def _retry_or_fail_text_task(self, run: dict[str, Any], task: dict[str, Any], message: str, only_keys: list[str] | None = None) -> None:
        """Release only unresolved tracks, so completed local songs are never regenerated."""
        tracks = run["tracks"]
        keys = only_keys or list(task["track_keys"])
        self.manifest.update(task, status="failed", retry_count=int(task.get("retry_count", 0)) + 1, error=message)
        for key in keys:
            track = tracks[key]
            if self._track_completed(track):
                continue
            retries = int(track.get("retry_count", 0)) + 1
            if retries >= self.config.max_retries:
                self.manifest.update(track, status="failed", retry_count=retries, error=message)
            else:
                self.manifest.update(track, status="pending", retry_count=retries, task_key=None, result_index=None, error=message)
        self.logger.error("%s: %s", task["task_key"], message)

    def _download_text_result(self, run: dict[str, Any], task: dict[str, Any], result: TaskResult) -> None:
        """Atomically download completed outputs and requeue only missing track positions."""
        tracks = run["tracks"]
        missing: list[str] = []
        for index, key in enumerate(task["track_keys"]):
            track = tracks[key]
            if self._track_completed(track):
                continue
            if index >= len(result.outputs) or not isinstance(result.outputs[index].get("file"), str):
                missing.append(key)
                continue
            try:
                self.client.download(result.outputs[index]["file"], Path(track["output_path"]))
            except AceApiError as exc:
                retries = int(task.get("retry_count", 0)) + 1
                if retries >= self.config.max_retries:
                    self._retry_or_fail_text_task(run, task, f"Download failed: {exc}")
                else:
                    self.manifest.update(task, status="download_pending", retry_count=retries, error=f"Download failed: {exc}")
                    self.manifest.update(track, status="download_pending", error=f"Download failed: {exc}")
                self.logger.error("%s: Download failed: %s", task["task_key"], exc)
                return
            self.manifest.update(track, status="completed", seed=str(result.outputs[index].get("seed_value", "")), error=None)
        if missing:
            self._retry_or_fail_text_task(run, task, f"Server returned only {len(result.outputs)}/{task['batch_size']} outputs", missing)
        else:
            self.manifest.update(task, status="completed", error=None)

    def _process_text_queries(self, run: dict[str, Any], selected: list[dict[str, Any]]) -> None:
        """Poll persisted text tasks and transition only their selected unfinished tracks."""
        selected_ids = {id(track) for track in selected}
        tasks = [task for task in run["tasks"].values() if task.get("task_id") and task.get("status") not in {"failed", "submission_uncertain", "completed"}
                 and any(id(run["tracks"][key]) in selected_ids for key in task["track_keys"])]
        if not tasks:
            return
        try:
            results = self.client.query([str(task["task_id"]) for task in tasks])
        except AceApiError as exc:
            self.logger.error("Batch status query failed: %s", exc)
            self._line(f"Status query temporarily failed: {exc}")
            return
        for task in tasks:
            result = results.get(str(task["task_id"]))
            if result is None:
                self._retry_or_fail_text_task(run, task, "Task is no longer present on ACE-Step server; only missing tracks will be resubmitted.")
            elif result.status == 1:
                self._download_text_result(run, task, result)
            elif result.status == 2:
                self._retry_or_fail_text_task(run, task, result.error or "ACE-Step task failed")
            else:
                self.manifest.update(task, status="running", error=None)
                for key in task["track_keys"]:
                    if not self._track_completed(run["tracks"][key]):
                        self.manifest.update(run["tracks"][key], status="running", error=None)

    def _text_progress(self, tracks: list[dict[str, Any]], audio_format: str) -> None:
        """Report true on-disk text2music completion without simulated percentages."""
        completed = sum(1 for track in tracks if self._track_completed(track))
        active = sum(1 for track in tracks if track.get("status") in {"queued", "running", "download_pending"})
        failed = sum(1 for track in tracks if track.get("status") == "failed")
        uncertain = sum(1 for track in tracks if track.get("status") == "submission_uncertain")
        self._line(f"Completed: {completed}  Active: {active}  Failed: {failed}  Uncertain: {uncertain}  Downloaded {audio_format.upper()}: {completed}")

    def _text_summary(self, tracks: list[dict[str, Any]], audio_format: str) -> int:
        """Print final track counts and return failure when any requested file is absent."""
        completed = sum(1 for track in tracks if self._track_completed(track))
        self._line("\nText2music summary")
        self._line(f"Requested tracks:   {len(tracks)}")
        self._line(f"Downloaded {audio_format.upper()}: {completed}")
        self._line(f"Completed tracks:   {completed}")
        self._line(f"Failed tracks:      {len(tracks) - completed}")
        for track in tracks:
            if not self._track_completed(track):
                self._line(f"  MISSING track_{int(track['ordinal']):04d}: {track.get('error') or track.get('status')}")
        return 0 if completed == len(tracks) else 1

    def _run_text2music(self) -> int:
        """Plan, submit, resume, poll, and download the requested no-reference tracks."""
        if self.count is None or self.count <= 0:
            raise ConfigError("--count must be a positive integer when --mode text2music")
        if self.limit is not None or self.selected_file is not None:
            raise ConfigError("--limit and --file are only valid when --mode remix")
        config = self.config.text2music
        if config is None:
            raise ConfigError("config.json is missing required text2music configuration")
        label = safe_stem(self.text_label) if self.text_label else None
        run_fingerprint = text2music_run_fingerprint(config, label)
        folder_name = f"{label}__{run_fingerprint[:8]}" if label else run_fingerprint[:8]
        output_dir = self.root / "outputs" / "text2music" / folder_name
        run = self.manifest.text2music_run(run_fingerprint, asdict(config), output_dir)
        if label:
            self.manifest.update(run, label=label)
        tracks = self._ensure_text_tracks(run, self.count, config)
        self.manifest.save()
        self._line("ACE Batch Text2Music\n")
        self._line(f"Server: ONLINE\nRequested tracks: {self.count}\nTracks/task: {config.batch_size}\nFormat: {config.audio_format.upper()}\n")
        while True:
            self._release_pending_text_tracks(run, tracks, config)
            for task in run["tasks"].values():
                if task.get("status") == "pending" and not task.get("task_id"):
                    self._submit_text_task(run, task, config)
            self.manifest.save()
            self._process_text_queries(run, tracks)
            self.manifest.save()
            self._text_progress(tracks, config.audio_format)
            if all(self._track_completed(track) or track.get("status") in {"failed", "submission_uncertain"} for track in tracks):
                break
            time.sleep(self.config.poll_interval_seconds)
        self.manifest.save()
        return self._text_summary(tracks, config.audio_format)

    # ---- lifecycle and Remix dispatcher -------------------------------------

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
            if self.mode not in {"remix", "text2music"}:
                raise ConfigError("--mode must be 'remix' or 'text2music'")
            if self.mode == "text2music" and (self.count is None or self.count <= 0):
                raise ConfigError("--count must be a positive integer when --mode text2music")
            if self.mode == "text2music" and (self.limit is not None or self.selected_file is not None):
                raise ConfigError("--limit and --file are only valid when --mode remix")
            if self.mode == "remix" and self.count is not None:
                raise ConfigError("--count is only valid when --mode text2music")
            if self.mode == "remix" and self.text_label is not None:
                raise ConfigError("--label is only valid when --mode text2music")
            self.config = load_config(self.root / "config.json", allow_placeholder_caption=self.caption_override is not None)
            if self.caption_override is not None:
                caption = self.caption_override.strip()
                if not caption or caption == "CHANGE_ME":
                    raise ConfigError("--caption must be a non-placeholder caption")
                if self.mode == "text2music":
                    if self.config.text2music is None:
                        raise ConfigError("config.json is missing required text2music configuration")
                    self.config = replace(self.config, text2music=replace(self.config.text2music, music_caption=caption))
                else:
                    self.config = replace(self.config, music_caption=caption)
            self.manifest.load()
        except (ConfigError, RuntimeError) as exc:
            self._line(f"Configuration/state error: {exc}")
            return 2
        (self.root / "outputs").mkdir(exist_ok=True)
        self.client = AceClient(self.config)
        try:
            self.client.health()
        except AceApiError as exc:
            self._line("ACE-Step API Server is unavailable.\n")
            self._line(f"Expected: {self.config.server_url}")
            self._line("\nPlease make sure:\n1. Remote ACE-Step API Server is running\n2. SSH Tunnel is connected")
            self.logger.error("Health check failed: %s", exc)
            return 2
        try:
            return self._run_text2music() if self.mode == "text2music" else self._run_remix()
        except ConfigError as exc:
            self._line(f"Input selection error: {exc}")
            return 2

    def _run_remix(self) -> int:
        sources = self._discover_sources()
        if not sources:
            self._line("No supported audio found in input/. Add .mp3, .wav, or .flac files and run again.")
            return 0
        self._line("ACE Batch Remix\n")
        self._line(f"Server: ONLINE\nInput songs: {len(sources)}\nVariants/song: {self.config.batch_size}\nExpected outputs: {len(sources) * self.config.batch_size}\n")
        self._line(f"Remix Strength: {self.config.remix_strength}\nCover Strength: {self.config.cover_strength}\nFormat: {self.config.audio_format.upper()}\n")
        if self.caption_override is not None:
            self._line("Caption: TEMPORARY CLI override (config.json was not changed)\n")
        used_dirs: dict[str, str] = {}
        run_fingerprint = remix_run_fingerprint(self.config)
        records: list[dict[str, Any]] = []
        for source in sources:
            try:
                fingerprint = sha256_file(source)
            except OSError as exc:
                self._line(f"Cannot read {source.name}: {exc}")
                continue
            folder, paths = self._output_paths(source, fingerprint, run_fingerprint, used_dirs)
            record = self.manifest.record(f"{fingerprint}:{run_fingerprint}", fingerprint, source, folder, paths, run_fingerprint)
            if self._outputs_exist(record):
                self.manifest.update(record, status="completed", error=None)
            records.append(record)
        self.manifest.save()
        while True:
            for record in records:
                if self._outputs_exist(record) or record.get("status") in {"failed", "submission_uncertain"}:
                    continue
                if not record.get("task_id"):
                    self._submit(record)
            self.manifest.save()
            active = [record for record in records if record.get("task_id") and not self._outputs_exist(record) and record.get("status") != "failed"]
            if active:
                self._process_query(active)
                self.manifest.save()
            self._progress(records)
            if all(self._outputs_exist(record) or record.get("status") in {"failed", "submission_uncertain"} for record in records):
                break
            time.sleep(self.config.poll_interval_seconds)
        self.manifest.save()
        return self._summary(records)
