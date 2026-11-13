#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v cmake >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
mkdir -p local
archive=local/whisper-v1.9.5.tar.gz
model=local/ggml-large-v3-turbo-q5_0.bin
if [ ! -f "$archive" ]; then
  curl -fLsS --max-time 120 https://codeload.github.com/ggml-org/whisper.cpp/tar.gz/refs/tags/v1.9.5 -o "$archive"
fi
echo "ff1a9053feb509ff9d7729703355541ae9690073a6b1c40eb692c962e0dc1720  $archive" | shasum -a 256 -c -
if [ ! -d local/whisper.cpp-1.9.5 ]; then
  tar -xzf "$archive" -C local
fi
cmake -S local/whisper.cpp-1.9.5 -B local/build \
  -DGGML_METAL=ON -DWHISPER_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build local/build --config Release --target whisper-cli -j 6
if [ ! -f "$model" ]; then
  curl -fLsS --max-time 600 \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q5_0.bin \
    -o "$model"
fi
echo "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2  $model" | shasum -a 256 -c -
local/build/bin/whisper-cli --version
