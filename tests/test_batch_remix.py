"""Mock-API regression tests for ACE Batch Remix request, recovery and output behavior."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import requests
import tempfile
from threading import Thread
from unittest.mock import patch
import unittest

from src.api import AceClient, AceSubmissionUncertain, build_remix_payload
from src.config import AppConfig, load_config
from src.runner import BatchRemixRunner, remix_run_fingerprint, safe_stem, sha256_file


class MockAceServer:
    def __init__(
        self,
        *,
        health_ok: bool = True,
        lose_first_task: bool = False,
        fail_download_attempts: int = 0,
        fail_all_tasks: bool = False,
    ) -> None:
        self.state = {
            "submitted": [],
            "queries": [],
            "downloads": 0,
            "health_ok": health_ok,
            "lose_first_task": lose_first_task,
            "fail_download_attempts": fail_download_attempts,
            "fail_all_tasks": fail_all_tasks,
        }
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def _json(self, data: object, status: int = 200) -> None:
                payload = json.dumps({"data": data, "code": status, "error": None}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    if state["health_ok"]:
                        self._json({"status": "ok"})
                    else:
                        self.send_response(503)
                        self.end_headers()
                elif self.path.startswith("/v1/audio"):
                    state["downloads"] += 1
                    if state["downloads"] <= state["fail_download_attempts"]:
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b"temporary outage")
                        return
                    audio = b"ID3mock-mp3-data"
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(audio)))
                    self.end_headers()
                    self.wfile.write(audio)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/release_task":
                    task_id = f"task-{len(state['submitted']) + 1}"
                    state["submitted"].append(body.decode("utf-8", errors="replace"))
                    self._json({"task_id": task_id, "status": "queued"})
                elif self.path == "/query_result":
                    requested = json.loads(body.decode())["task_id_list"]
                    state["queries"].append(requested)
                    response = []
                    for task_id in requested:
                        if state["lose_first_task"] and task_id == "legacy-task":
                            continue
                        if state["fail_all_tasks"]:
                            response.append({"task_id": task_id, "status": 2, "error": "mock inference failure"})
                            continue
                        response.append(
                            {
                                "task_id": task_id,
                                "status": 1,
                                "result": json.dumps(
                                    [
                                        {"file": "/v1/audio?path=one.mp3", "seed_value": "101"},
                                        {"file": "/v1/audio?path=two.mp3", "seed_value": "202"},
                                        {"file": "/v1/audio?path=three.mp3", "seed_value": "303"},
                                        {"file": "/v1/audio?path=four.mp3", "seed_value": "404"},
                                    ]
                                ),
                            }
                        )
                    self._json(response)
                else:
                    self.send_error(404)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "MockAceServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def write_config(root: Path, server_url: str, max_retries: int = 3, batch_size: int = 2) -> None:
    root.joinpath("config.json").write_text(
        json.dumps(
            {
                "server_url": server_url,
                "music_caption": "city pop, bright guitars",
                "generation_mode": "remix",
                "remix_strength": 1.0,
                "cover_strength": 0.2,
                "batch_size": batch_size,
                "audio_format": "mp3",
                "use_random_seed": True,
                "poll_interval_seconds": 0.001,
                "max_retries": max_retries,
                "request_timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )


class BatchRemixTests(unittest.TestCase):
    def test_payload_keeps_all_remix_mapping_in_one_place(self) -> None:
        config = AppConfig("http://127.0.0.1:8001", "target style", "remix", 1.0, 0.2, 2, "mp3", True, 5, 3, 60)
        self.assertEqual(
            build_remix_payload(config),
            {
                "task_type": "cover",
                "prompt": "target style",
                "lyrics": "",
                "audio_cover_strength": "1.0",
                "cover_noise_strength": "0.2",
                "batch_size": "2",
                "audio_format": "mp3",
                "use_random_seed": "true",
            },
        )

    def test_submit_query_download_unicode_and_seed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "宇多田ヒカル.mp3").write_bytes(b"source")
            write_config(root, server.url)
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
            record = next(iter(manifest["songs"].values()))
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["seeds"], ["101", "202"])
            self.assertTrue(all(Path(item).is_file() for item in record["output_paths"]))
            submitted = server.state["submitted"][0]
            self.assertIn('name="src_audio"; filename="宇多田ヒカル.mp3"', submitted)
            self.assertIn('name="task_type"', submitted)
            self.assertIn("cover", submitted)
            self.assertIn("audio_cover_strength", submitted)

    def test_restart_lost_task_resubmits_only_that_song(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer(lose_first_task=True) as server:
            root = Path(temp)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "song.mp3"
            source.write_bytes(b"source")
            write_config(root, server.url)
            runner = BatchRemixRunner(root)
            fingerprint = sha256_file(source)
            config = load_config(root / "config.json")
            runner.config = config
            run_fingerprint = remix_run_fingerprint(config)
            folder, paths = runner._output_paths(source, fingerprint, run_fingerprint, {})
            identity = f"{fingerprint}:{run_fingerprint}"
            runner.manifest.record(identity, fingerprint, source, folder, paths, run_fingerprint)
            record = runner.manifest.data["songs"][identity]
            record.update({"task_id": "legacy-task", "status": "running"})
            runner.manifest.save()
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            self.assertEqual(len(server.state["submitted"]), 1)
            self.assertTrue(any("legacy-task" in query for query in server.state["queries"]))

    def test_download_failure_retries_existing_task_and_cleans_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer(fail_download_attempts=3) as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "song.mp3").write_bytes(b"source")
            write_config(root, server.url)
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            self.assertEqual(len(server.state["submitted"]), 1)
            self.assertGreaterEqual(server.state["downloads"], 5)
            self.assertFalse(any(root.joinpath("outputs").rglob("*.part")))

    def test_completed_outputs_are_skipped_without_resubmission(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "song.mp3").write_bytes(b"source")
            write_config(root, server.url)
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            self.assertEqual(len(server.state["submitted"]), 1)

    def test_unavailable_health_prevents_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer(health_ok=False) as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "song.mp3").write_bytes(b"source")
            write_config(root, server.url)
            self.assertEqual(BatchRemixRunner(root).run(), 2)
            self.assertEqual(server.state["submitted"], [])

    def test_failed_task_stops_after_retry_limit_without_affecting_exit_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer(fail_all_tasks=True) as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "song.mp3").write_bytes(b"source")
            write_config(root, server.url, max_retries=1)
            self.assertEqual(BatchRemixRunner(root).run(), 1)
            manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
            record = next(iter(manifest["songs"].values()))
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["retry_count"], 1)
            self.assertEqual(len(server.state["submitted"]), 1)

    def test_safe_stem_and_output_dir_collision_are_windows_safe(self) -> None:
        self.assertEqual(safe_stem('a<b>:c*?'), "a_b__c__")
        runner = BatchRemixRunner(Path("C:/temporary-root"))
        runner.config = AppConfig("http://127.0.0.1:8001", "caption", "remix", 1.0, 0.2, 4, "mp3", True, 5, 3, 60)
        used: dict[str, str] = {}
        first, first_paths = runner._output_paths(Path("歌.mp3"), "a" * 64, "c" * 64, used)
        second, second_paths = runner._output_paths(Path("歌.wav"), "b" * 64, "c" * 64, used)
        self.assertEqual(first.name, "歌__cccccccc")
        self.assertEqual(second.name, "歌_bbbbbbbb__cccccccc")
        self.assertEqual(len(first_paths), 4)
        self.assertEqual(len(second_paths), 4)

    def test_batch_size_four_creates_four_outputs_in_a_settings_scoped_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            root.joinpath("input").mkdir()
            root.joinpath("input", "song.mp3").write_bytes(b"source")
            write_config(root, server.url, batch_size=4)
            self.assertEqual(BatchRemixRunner(root).run(), 0)
            manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
            record = next(iter(manifest["songs"].values()))
            self.assertEqual(len(record["output_paths"]), 4)
            self.assertTrue(all(Path(item).is_file() for item in record["output_paths"]))
            self.assertTrue(Path(record["output_dir"]).name.startswith("song__"))

    def test_limit_and_selected_file_choose_only_requested_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            input_dir.mkdir()
            first = input_dir / "a.mp3"
            second = input_dir / "b.mp3"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            self.assertEqual(BatchRemixRunner(root, limit=1)._discover_sources(), [first])
            self.assertEqual(BatchRemixRunner(root, selected_file="input/b.mp3")._discover_sources(), [second])
            with self.assertRaisesRegex(Exception, "under input"):
                BatchRemixRunner(root, selected_file="outside.mp3")._discover_sources()

    def test_submit_read_timeout_is_uncertain_and_not_a_retryable_api_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "song.mp3"
            source.write_bytes(b"source")
            config = AppConfig("http://127.0.0.1:8001", "caption", "remix", 1.0, 0.2, 2, "mp3", True, 5, 3, 60)
            client = AceClient(config)
            with patch.object(client.session, "post", side_effect=requests.ReadTimeout()):
                with self.assertRaises(AceSubmissionUncertain):
                    client.submit_remix(source)


if __name__ == "__main__":
    unittest.main()
