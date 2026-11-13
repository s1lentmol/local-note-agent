"""Isolated P03: FFmpeg decoding, whisper.cpp, full text/language only.

Python 3.9+, standard library only. No backend imports or cloud inference.
"""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / "local"


def run(command, timeout=600):
    result = subprocess.run(
        [str(arg) for arg in command], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        # Do not propagate private filenames, transcripts or raw runtime logs.
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode})")
    return result


def decode(source, target):
    """Validate container/codec and actually decode all frames, ignoring suffix."""
    metadata = json.loads(run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", source,
    ]).stdout)
    streams = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    if not streams:
        raise ValueError("No audio stream")
    codec = streams[0]["codec_name"]
    containers = set(metadata["format"]["format_name"].split(","))
    if "ogg" in containers and codec == "opus":
        kind = "ogg/opus"
    elif "wav" in containers and codec.startswith("pcm_"):
        kind = "wav"
    elif "mp3" in containers and codec == "mp3":
        kind = "mp3"
    else:
        raise ValueError("Expected OGG/Opus, PCM WAV or MP3 content")
    run([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", "-i", source,
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", target,
    ])
    with wave.open(str(target), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    if duration <= 0:
        raise ValueError("Empty decoded audio")
    return {"format": kind, "codec": codec, "duration_seconds": duration}


def recognize(wav, runtime, model):
    started = time.perf_counter()
    result = run([
        "/usr/bin/time", "-l", runtime, "-m", model, "-f", wav,
        "-l", "auto", "-t", "6", "-bs", "5", "-bo", "5",
        "-tp", "0", "-tpi", "0",  # deterministic temperature; no fallback
    ])
    elapsed = time.perf_counter() - started
    language = re.search(r"auto-detected language: ([a-z]+)\b", result.stderr)
    # Let Whisper align its internal windows. CLI -nt also disables internal
    # timestamp prediction and lost paragraphs on the long public sample.
    # Remove display prefixes in memory, before returning or saving anything.
    text = re.sub(
        r"(?m)^\s*\[\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+"
        r"\d{2}:\d{2}:\d{2}\.\d{3}\]\s*", "", result.stdout
    ).strip()
    if not language or not text:
        raise RuntimeError("ASR returned no text or detected language")
    rss = re.search(r"(\d+)\s+maximum resident set size", result.stderr)
    metrics = {
        "asr_seconds": elapsed,
        "maximum_rss_bytes": int(rss.group(1)) if rss else None,
        "metal_device_reported": bool(re.search(
            r"whisper_backend_init_gpu: using MTL\d+ backend", result.stderr)),
    }
    # No ASR JSON output: only cleaned text and language leave this function.
    return {"text": text, "language": language.group(1)}, metrics


def transcribe(source, runtime, model):
    """Python ASR contract: exactly text and detected language."""
    with tempfile.TemporaryDirectory(prefix="p03-") as temp:
        wav = Path(temp) / "decoded.wav"
        decode(source, wav)
        return recognize(wav, runtime, model)[0]


def words(text):
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def compare(reference, hypothesis):
    """Small word edit distance; spelling/case/punctuation normalization only."""
    expected, actual = words(reference), words(hypothesis)
    if not expected:
        raise ValueError("Empty control text")
    previous = list(range(len(actual) + 1))
    for i, word in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (word != other)))
        previous = current
    return {"reference_words": len(expected), "hypothesis_words": len(actual),
            "word_edits": previous[-1], "wer": previous[-1] / len(expected)}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument("--runtime", type=Path, default=LOCAL / "build/bin/whisper-cli")
    parser.add_argument("--model", type=Path, default=LOCAL / "ggml-large-v3-turbo-q5_0.bin")
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=LOCAL / "results.json")
    parser.add_argument("--smoke", action="store_true", help="Allow audio outside 2–5 minutes")
    args = parser.parse_args()
    # Personal results must stay in the ignored local directory.
    args.output.resolve().relative_to(LOCAL.resolve())
    reference = args.reference.read_text(encoding="utf-8")
    rows = []
    for source in args.audio:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="p03-") as temp:
            wav = Path(temp) / "decoded.wav"
            metadata = decode(source, wav)
            decode_seconds = time.perf_counter() - started
            if not args.smoke and not 120 <= metadata["duration_seconds"] <= 300:
                raise ValueError("P03 requires 120–300 seconds; use --smoke only for diagnostics")
            result, metrics = recognize(wav, args.runtime, args.model)
        rows.append({"audio": metadata, "result": result, "metrics": {
            **metrics, "decode_seconds": decode_seconds,
            "total_seconds": time.perf_counter() - started,
            "asr_rtf": metrics["asr_seconds"] / metadata["duration_seconds"],
        }, "quality": compare(reference, result["text"])})
    report = {"smoke_only": args.smoke, "model_sha256": sha256(args.model), "runs": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(rows)} run(s); smoke_only={args.smoke}")


if __name__ == "__main__":
    main()
