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

from src.api import AceClient, AceSubmissionUncertain, build_remix_payload, build_text2music_payload
from src.config import AppConfig, Text2MusicConfig, load_config
from src.manifest import ManifestStore
from src.runner import BatchRemixRunner, remix_run_fingerprint, safe_stem, sha256_file


class MockAceServer:
    def __init__(
        self,
        *,
        health_ok: bool = True,
        lose_first_task: bool = False,
        fail_download_attempts: int = 0,
        fail_all_tasks: bool = False,
        short_first_text_result: bool = False,
        lose_first_text_task: bool = False,
    ) -> None:
        self.state = {
            "submitted": [],
            "queries": [],
            "downloads": 0,
            "health_ok": health_ok,
            "lose_first_task": lose_first_task,
            "fail_download_attempts": fail_download_attempts,
            "fail_all_tasks": fail_all_tasks,
            "short_first_text_result": short_first_text_result,
            "lose_first_text_task": lose_first_text_task,
            "lost_text_task": False,
            "shortened": False,
            "submitted_json": [],
            "task_batches": {},
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
                    decoded = body.decode("utf-8", errors="replace")
                    state["submitted"].append(decoded)
                    if self.headers.get("Content-Type", "").startswith("application/json"):
                        payload = json.loads(decoded)
                        state["submitted_json"].append(payload)
                        state["task_batches"][task_id] = int(payload["batch_size"])
                    self._json({"task_id": task_id, "status": "queued"})
                elif self.path == "/query_result":
                    requested = json.loads(body.decode())["task_id_list"]
                    state["queries"].append(requested)
                    response = []
                    for task_id in requested:
                        if state["lose_first_task"] and task_id == "legacy-task":
                            continue
                        if state["lose_first_text_task"] and task_id == "task-1" and not state["lost_text_task"]:
                            state["lost_text_task"] = True
                            continue
                        if state["fail_all_tasks"]:
                            response.append({"task_id": task_id, "status": 2, "error": "mock inference failure"})
                            continue
                        batch_size = state["task_batches"].get(task_id, 4)
                        if state["short_first_text_result"] and task_id == "task-1" and not state["shortened"]:
                            batch_size -= 1
                            state["shortened"] = True
                        response.append(
                            {
                                "task_id": task_id,
                                "status": 1,
                                "result": json.dumps(
                                    [
                                        {"file": f"/v1/audio?path={task_id}-{index}.audio", "seed_value": str(101 * index)}
                                        for index in range(1, batch_size + 1)
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


def write_config(
    root: Path,
    server_url: str,
    max_retries: int = 3,
    batch_size: int = 2,
    text2music: dict[str, object] | None = None,
) -> None:
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
                **({"text2music": text2music} if text2music is not None else {}),
            }
        ),
        encoding="utf-8",
    )


class BatchRemixTests(unittest.TestCase):
    def test_v1_manifest_loads_without_changing_legacy_songs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "manifest.json")
            legacy_songs = {"legacy": {"source_filename": "song.mp3", "status": "running"}}
            path.write_text(json.dumps({"version": 1, "songs": legacy_songs}), encoding="utf-8")
            store = ManifestStore(path)
            store.load()
            self.assertEqual(store.data["version"], 2)
            self.assertEqual(store.data["songs"], legacy_songs)
            self.assertEqual(store.data["text2music_runs"], {})

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

    def test_text2music_payload_is_json_only_and_instrumental(self) -> None:
        config = Text2MusicConfig("warm lo-fi beats", 180, True, True, 8, "flac", 4, False, 42)
        self.assertEqual(
            build_text2music_payload(config, 3, 44),
            {
                "task_type": "text2music",
                "prompt": "warm lo-fi beats",
                "lyrics": "[Instrumental]",
                "instrumental": True,
                "audio_duration": 180,
                "thinking": True,
                "inference_steps": 8,
                "audio_format": "flac",
                "batch_size": 3,
                "use_random_seed": False,
                "seed": 44,
            },
        )

    def test_text2music_batches_twenty_flac_and_resumes_before_extending(self) -> None:
        text_config = {
            "music_caption": "lo-fi instrumental, warm vinyl crackle",
            "audio_duration": 120,
            "instrumental": True,
            "thinking": True,
            "inference_steps": 8,
            "audio_format": "flac",
            "batch_size": 4,
            "use_random_seed": True,
            "seed": -1,
        }
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            write_config(root, server.url, text2music=text_config)
            self.assertFalse(root.joinpath("input").exists())
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=20).run(), 0)
            self.assertFalse(root.joinpath("input").exists())
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [4, 4, 4, 4, 4])
            manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
            run = next(iter(manifest["text2music_runs"].values()))
            self.assertEqual(len(run["tracks"]), 20)
            self.assertTrue(all(Path(track["output_path"]).suffix == ".flac" and Path(track["output_path"]).is_file() for track in run["tracks"].values()))
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=20).run(), 0)
            self.assertEqual(len(server.state["submitted_json"]), 5)
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=22).run(), 0)
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [4, 4, 4, 4, 4, 2])

    def test_text2music_wav_and_fixed_seeds_are_derived_from_first_track(self) -> None:
        text_config = {
            "music_caption": "quiet lo-fi piano",
            "audio_duration": 60,
            "instrumental": True,
            "thinking": False,
            "inference_steps": 8,
            "audio_format": "wav",
            "batch_size": 4,
            "use_random_seed": False,
            "seed": 100,
        }
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            write_config(root, server.url, text2music=text_config)
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=9).run(), 0)
            self.assertEqual([item["seed"] for item in server.state["submitted_json"]], [100, 104, 108])
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [4, 4, 1])
            manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
            run = next(iter(manifest["text2music_runs"].values()))
            self.assertTrue(all(Path(track["output_path"]).suffix == ".wav" for track in run["tracks"].values()))

    def test_text2music_batch_size_one_uses_one_task_for_each_track(self) -> None:
        text_config = {
            "music_caption": "single-task lo-fi", "audio_duration": 60, "instrumental": True,
            "thinking": False, "inference_steps": 8, "audio_format": "flac",
            "batch_size": 1, "use_random_seed": True, "seed": -1,
        }
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            write_config(root, server.url, text2music=text_config)
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=4).run(), 0)
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [1, 1, 1, 1])

    def test_text2music_replaces_only_missing_track_after_partial_server_result(self) -> None:
        text_config = {
            "music_caption": "lo-fi drum loop", "audio_duration": 60, "instrumental": True,
            "thinking": False, "inference_steps": 8, "audio_format": "flac",
            "batch_size": 2, "use_random_seed": False, "seed": 7,
        }
        with tempfile.TemporaryDirectory() as temp, MockAceServer(short_first_text_result=True) as server:
            root = Path(temp)
            write_config(root, server.url, text2music=text_config)
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=2).run(), 0)
            # The first response completed track 1. The replacement is a one-track
            # request, proving the successful local song was not regenerated.
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [2, 1])
            self.assertEqual([item["seed"] for item in server.state["submitted_json"]], [7, 8])

    def test_text2music_lost_task_resubmits_only_unfinished_tracks(self) -> None:
        text_config = {
            "music_caption": "lo-fi drum loop", "audio_duration": 60, "instrumental": True,
            "thinking": False, "inference_steps": 8, "audio_format": "flac",
            "batch_size": 2, "use_random_seed": False, "seed": 7,
        }
        with tempfile.TemporaryDirectory() as temp, MockAceServer(lose_first_text_task=True) as server:
            root = Path(temp)
            write_config(root, server.url, text2music=text_config)
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=2).run(), 0)
            self.assertEqual([item["batch_size"] for item in server.state["submitted_json"]], [2, 2])
            self.assertEqual([item["seed"] for item in server.state["submitted_json"]], [7, 7])

    def test_text2music_rejects_invalid_count_without_creating_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp, MockAceServer() as server:
            root = Path(temp)
            write_config(root, server.url, text2music={
                "music_caption": "lo-fi", "audio_duration": 60, "instrumental": True,
                "thinking": False, "inference_steps": 8, "audio_format": "flac",
                "batch_size": 4, "use_random_seed": True, "seed": -1,
            })
            self.assertEqual(BatchRemixRunner(root, mode="text2music", count=0).run(), 2)
            self.assertFalse(root.joinpath("input").exists())

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
