"""Local FFmpeg-backed playlist concatenation with safe, atomic WAV or FLAC output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


SUPPORTED_AUDIO_EXTENSIONS = {".flac", ".mp3", ".wav"}
CONCAT_OUTPUT_FORMATS = {
    "flac": {"suffix": ".flac", "codec": "flac"},
    "wav": {"suffix": ".wav", "codec": "pcm_s16le"},
}
ILLEGAL_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class AudioConcatError(ValueError):
    """Raised when a playlist, output destination, or FFmpeg invocation is invalid."""


@dataclass(frozen=True)
class ConcatenationResult:
    """Describe the completed local output and the sources included in it."""

    output_path: Path
    sources: tuple[Path, ...]


def load_playlist(playlist_path: Path) -> list[Path]:
    """Read one UTF-8 path per line, resolving relative entries beside the playlist."""
    try:
        lines = playlist_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError as exc:
        raise AudioConcatError(f"Playlist file not found: {playlist_path}") from exc
    except OSError as exc:
        raise AudioConcatError(f"Cannot read playlist {playlist_path}: {exc}") from exc

    sources: list[Path] = []
    for line_number, raw in enumerate(lines, start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = playlist_path.parent / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise AudioConcatError(f"Playlist line {line_number} is not a readable file: {candidate}")
        if candidate.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
            raise AudioConcatError(f"Playlist line {line_number} must be one of {allowed}: {candidate.name}")
        sources.append(candidate)
    if not sources:
        raise AudioConcatError("Playlist contains no supported audio files.")
    return sources


def concat_output_format(output_format: str) -> dict[str, str]:
    """Validate a requested local output format and return its FFmpeg codec and filename suffix."""
    normalized = output_format.lower()
    try:
        return CONCAT_OUTPUT_FORMATS[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(CONCAT_OUTPUT_FORMATS))
        raise AudioConcatError(f"--output-format must be one of: {allowed}") from exc


def concat_output_path(root: Path, label: str, output_format: str = "wav") -> Path:
    """Allocate a Windows-safe destination for the selected local output format."""
    normalized = ILLEGAL_WINDOWS_NAME.sub("_", label).strip(". ")
    if not normalized:
        raise AudioConcatError("--label must contain at least one valid filename character.")
    return root / "outputs" / "concat" / f"{normalized}{concat_output_format(output_format)['suffix']}"


def resolve_ffmpeg(ffmpeg_bin: str) -> str:
    """Resolve an explicit FFmpeg executable or a command available on PATH before processing audio."""
    candidate = Path(ffmpeg_bin)
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(ffmpeg_bin)
    if resolved:
        return resolved
    raise AudioConcatError(
        f"FFmpeg executable was not found: {ffmpeg_bin}. Install FFmpeg on PATH or pass --ffmpeg-bin with its full path."
    )


def build_concat_command(ffmpeg_bin: str, sources: list[Path], part_path: Path, output_format: str = "wav") -> list[str]:
    """Build an FFmpeg concat-filter command that normalizes formats without changing loudness."""
    format_details = concat_output_format(output_format)
    inputs = [item for source in sources for item in ("-i", str(source))]
    per_source = [
        f"[{index}:a]aformat=sample_rates=48000:sample_fmts=s16:channel_layouts=stereo[a{index}]"
        for index in range(len(sources))
    ]
    concat_inputs = "".join(f"[a{index}]" for index in range(len(sources)))
    filter_graph = ";".join(per_source + [f"{concat_inputs}concat=n={len(sources)}:v=0:a=1[outa]"])
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        *inputs,
        "-filter_complex",
        filter_graph,
        "-map",
        "[outa]",
        "-c:a",
        format_details["codec"],
        str(part_path),
    ]


class AudioConcatRunner:
    """Concatenate a local playlist without contacting ACE-Step or loading generation configuration."""

    def __init__(self, root: Path, *, playlist: str, label: str, ffmpeg_bin: str = "ffmpeg",
                 output_format: str = "wav") -> None:
        """Keep explicit CLI inputs for a single non-resumable local concatenation run."""
        self.root = root
        playlist_path = Path(playlist)
        self.playlist = (playlist_path if playlist_path.is_absolute() else root / playlist_path).resolve()
        self.label = label
        self.ffmpeg_bin = ffmpeg_bin
        self.output_format = output_format

    def run(self) -> int:
        """Validate inputs, write a temporary WAV or FLAC through FFmpeg, and atomically publish the completed mix."""
        try:
            sources = load_playlist(self.playlist)
            output_path = concat_output_path(self.root, self.label, self.output_format)
            if output_path.exists():
                raise AudioConcatError(f"Output already exists and will not be overwritten: {output_path}")
            executable = resolve_ffmpeg(self.ffmpeg_bin)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            part_path = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
            part_path.unlink(missing_ok=True)
            completed = subprocess.run(
                build_concat_command(executable, sources, part_path, self.output_format),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-1200:] or "FFmpeg did not provide an error message."
                raise AudioConcatError(f"FFmpeg failed while concatenating audio:\n{detail}")
            if not part_path.is_file() or part_path.stat().st_size == 0:
                raise AudioConcatError(
                    f"FFmpeg reported success but did not create a non-empty {self.output_format.upper()} output."
                )
            part_path.rename(output_path)
        except (AudioConcatError, OSError) as exc:
            try:
                if "part_path" in locals():
                    part_path.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"Audio concatenation error: {exc}", flush=True)
            return 2

        result = ConcatenationResult(output_path=output_path, sources=tuple(sources))
        print("Audio concatenation completed\n", flush=True)
        print(f"Input songs: {len(result.sources)}\nOutput: {result.output_path}", flush=True)
        return 0
