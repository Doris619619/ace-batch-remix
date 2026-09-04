"""Regression tests for local playlist parsing and safe FFmpeg concatenation."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest

from batch_remix import parse_args
from src.audio_concat import (
    AudioConcatRunner,
    build_concat_command,
    concat_output_path,
    load_playlist,
)


class AudioConcatTests(unittest.TestCase):
    """Exercise local-only concatenation without requiring a real FFmpeg installation."""

    def _audio(self, root: Path, name: str) -> Path:
        """Create a small placeholder source whose name controls the tested audio extension."""
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-audio")
        return path

    def test_cli_requires_concat_playlist_and_label_and_rejects_generation_flags(self) -> None:
        """Expose concat's dedicated arguments without allowing unrelated ACE-Step controls."""
        with patch.object(sys, "argv", ["batch_remix.py", "--mode", "concat", "--playlist", "mix.txt", "--label", "mix", "--ffmpeg-bin", "C:/ffmpeg/ffmpeg.exe", "--output-format", "flac"]):
            args = parse_args()
        self.assertEqual(args.playlist, "mix.txt")
        self.assertEqual(args.ffmpeg_bin, "C:/ffmpeg/ffmpeg.exe")
        self.assertEqual(args.output_format, "flac")
        with patch.object(sys, "argv", ["batch_remix.py", "--mode", "concat", "--playlist", "mix.txt"]):
            with self.assertRaises(SystemExit):
                parse_args()
        with patch.object(sys, "argv", ["batch_remix.py", "--mode", "concat", "--playlist", "mix.txt", "--label", "mix", "--count", "2"]):
            with self.assertRaises(SystemExit):
                parse_args()
        with patch.object(sys, "argv", ["batch_remix.py", "--mode", "remix", "--output-format", "wav"]):
            with self.assertRaises(SystemExit):
                parse_args()

    def test_playlist_preserves_unicode_order_duplicates_and_relative_paths(self) -> None:
        """Load supported entries in source order while skipping comments and blank lines."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._audio(root, "音乐/第一首.flac")
            second = self._audio(root, "音乐/second.mp3")
            playlist = root / "playlists" / "mix.txt"
            playlist.parent.mkdir()
            playlist.write_text("\ufeff# opening\n../音乐/第一首.flac\n\n../音乐/second.mp3\n../音乐/第一首.flac\n", encoding="utf-8")
            self.assertEqual(load_playlist(playlist), [first.resolve(), second.resolve(), first.resolve()])

    def test_playlist_reports_empty_missing_and_unsupported_entries(self) -> None:
        """Reject unusable lists before FFmpeg is started or any output directory is created."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / "empty.txt"
            empty.write_text("# no tracks\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "no supported audio"):
                load_playlist(empty)
            missing = root / "missing.txt"
            missing.write_text("nope.flac\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "line 1"):
                load_playlist(missing)
            source = self._audio(root, "source.ogg")
            unsupported = root / "unsupported.txt"
            unsupported.write_text(f"{source}\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "must be one of"):
                load_playlist(unsupported)

    def test_command_normalizes_each_source_without_loudness_filter(self) -> None:
        """Build a concat-filter command that preserves order and avoids gain or loudness processing."""
        first = Path("C:/audio/first.mp3")
        second = Path("C:/audio/second.wav")
        command = build_concat_command("ffmpeg.exe", [first, second], Path("C:/output/.mix.part.wav"))
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual([command[index + 1] for index, value in enumerate(command) if value == "-i"], [str(first), str(second)])
        self.assertIn("concat=n=2:v=0:a=1[outa]", graph)
        self.assertIn("sample_rates=48000", graph)
        self.assertNotIn("loudnorm", graph)
        self.assertNotIn("afade", graph)
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
        self.assertTrue(command[-1].endswith(".wav"))

    def test_runner_publishes_nonempty_part_atomically_and_never_overwrites(self) -> None:
        """Publish only a successful temporary WAV and reject a subsequent same-label run."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._audio(root, "song.flac")
            playlist = root / "mix.txt"
            playlist.write_text(f"{source}\n", encoding="utf-8")

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                """Simulate FFmpeg by writing the command's final temporary output path."""
                Path(command[-1]).write_bytes(b"WAV")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("src.audio_concat.resolve_ffmpeg", return_value="ffmpeg.exe"), patch("src.audio_concat.subprocess.run", side_effect=fake_run):
                self.assertEqual(AudioConcatRunner(root, playlist="mix.txt", label="my mix").run(), 0)
            output = concat_output_path(root, "my mix")
            self.assertEqual(output.read_bytes(), b"WAV")
            self.assertEqual(output.suffix, ".wav")
            self.assertFalse((output.parent / ".my mix.part.wav").exists())
            self.assertEqual(AudioConcatRunner(root, playlist="mix.txt", label="my mix").run(), 2)

    def test_runner_cleans_failed_temporary_output_and_reports_missing_ffmpeg(self) -> None:
        """Leave no partial file after FFmpeg failure and give a clear missing-engine error."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._audio(root, "song.wav")
            playlist = root / "mix.txt"
            playlist.write_text(f"{source}\n", encoding="utf-8")

            def failing_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                """Simulate an engine that writes an incomplete file before exiting unsuccessfully."""
                Path(command[-1]).write_bytes(b"incomplete")
                return subprocess.CompletedProcess(command, 1, "", "decoder failed")

            with patch("src.audio_concat.resolve_ffmpeg", return_value="ffmpeg.exe"), patch("src.audio_concat.subprocess.run", side_effect=failing_run):
                self.assertEqual(AudioConcatRunner(root, playlist="mix.txt", label="failed").run(), 2)
            self.assertFalse((root / "outputs" / "concat" / ".failed.part.wav").exists())
            with patch("src.audio_concat.shutil.which", return_value=None):
                self.assertEqual(AudioConcatRunner(root, playlist="mix.txt", label="no-engine", ffmpeg_bin="missing-ffmpeg").run(), 2)

    def test_explicit_flac_keeps_the_existing_lossless_compressed_output(self) -> None:
        """Allow callers to override WAV's default with the prior FLAC encoding and extension."""
        command = build_concat_command("ffmpeg.exe", [Path("C:/audio/song.wav")], Path("C:/output/.mix.part.flac"), "flac")
        self.assertEqual(command[command.index("-c:a") + 1], "flac")
        self.assertEqual(concat_output_path(Path("C:/output"), "mix", "flac").suffix, ".flac")
