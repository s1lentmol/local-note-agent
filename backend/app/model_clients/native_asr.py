"""Native host only: FFmpeg validation and the pinned P03 whisper.cpp CLI."""

import json
import re
import subprocess
import tempfile
import wave
from pathlib import Path

from .contracts import ModelClientError, Transcript


def run(command, timeout):
    try:
        result = subprocess.run(
            [str(x) for x in command], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise ModelClientError("asr_runtime_timeout") from None
    except OSError:
        raise ModelClientError("asr_runtime_unavailable") from None
    if result.returncode:
        raise ModelClientError("asr_runtime_failed")
    return result


def decode(source, target, timeout, max_seconds):
    try:
        metadata = json.loads(
            run(
                ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", source],
                timeout,
            ).stdout
        )
        stream = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
        codec = stream["codec_name"]
        containers = set(metadata["format"]["format_name"].split(","))
        if not (
            ("ogg" in containers and codec == "opus")
            or ("wav" in containers and codec.startswith("pcm_"))
            or ("mp3" in containers and codec == "mp3")
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, StopIteration):
        raise ModelClientError("invalid_audio_format") from None
    # Decode one extra second so oversized audio is rejected, never silently clipped.
    run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-y",
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            str(max_seconds + 1),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            target,
        ],
        timeout,
    )
    try:
        with wave.open(str(target), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if not 0 < duration <= max_seconds:
            raise ValueError()
    except (wave.Error, OSError, ValueError, ZeroDivisionError):
        raise ModelClientError("invalid_audio_duration") from None


def clean_transcript(stdout, stderr):
    language = re.search(r"auto-detected language: ([a-z]{2,3})\b", stderr)
    text = re.sub(
        r"(?m)^\s*\[\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+"
        r"\d{2}:\d{2}:\d{2}\.\d{3}\]\s*",
        "",
        stdout,
    ).strip()
    if not language or not text:
        raise ModelClientError("empty_asr_result")
    return Transcript(text=text, language=language.group(1))


class NativeWhisperTranscriber:
    def __init__(self, runtime: Path, model: Path, *, timeout=600, max_seconds=1800):
        if not runtime.is_file() or not model.is_file():
            raise ModelClientError("asr_runtime_unavailable")
        self.runtime, self.model = runtime, model
        self.timeout, self.max_seconds = timeout, max_seconds

    def transcribe(self, audio):
        with tempfile.TemporaryDirectory(prefix="asr-") as temp:
            wav = Path(temp) / "decoded.wav"
            decode(audio, wav, self.timeout, self.max_seconds)
            # Keep internal alignment: -nt lost paragraphs in P03.
            result = run(
                [
                    self.runtime,
                    "-m",
                    self.model,
                    "-f",
                    wav,
                    "-l",
                    "auto",
                    "-t",
                    "6",
                    "-bs",
                    "5",
                    "-bo",
                    "5",
                    "-tp",
                    "0",
                    "-tpi",
                    "0",
                ],
                self.timeout,
            )
            return clean_transcript(result.stdout, result.stderr)
