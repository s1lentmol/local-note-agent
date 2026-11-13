#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p local
if [ ! -f local/public-original.mp3 ]; then
  curl -fLsS --max-time 120 \
    https://archive.org/download/chelovek_2103_librivox/chelovekvfutliare_03_chekhov.mp3 \
    -o local/public-original.mp3
fi
echo 'f6489051c795c73cb49b319c02ad587d90ef5cb1a0519f293a64bcdfb4b4d2a7  local/public-original.mp3' | shasum -a 256 -c -
