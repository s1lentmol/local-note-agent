"""Make comparable formats; keep an original Telegram OGG/Opus unchanged."""
import argparse
import shutil
from pathlib import Path

from probe import LOCAL, decode, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    target = LOCAL / "audio"
    if args.source.resolve().parent == target.resolve():
        raise ValueError("Keep original outside the generated local/audio directory")
    target.mkdir(parents=True, exist_ok=True)
    wav = target / "voice.wav"
    metadata = decode(args.source, wav)
    ogg = target / "voice.ogg"
    if metadata["format"] == "ogg/opus":
        shutil.copyfile(args.source, ogg)
    else:
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", wav,
             "-ar", "48000", "-c:a", "libopus", "-b:a", "32k",
             "-application", "voip", ogg])
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", wav,
         "-c:a", "libmp3lame", "-b:a", "96k", target / "voice.mp3"])
    print("Prepared WAV, OGG/Opus, MP3 in ignored local/audio")


if __name__ == "__main__":
    main()
